"""`route.py run`: leases and slots, who runs unattended, history and status, limits and resume."""
import dataclasses
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (a home of its own for every test)
import routing_support as support
from run_support import RunBase
from stream_proc import proc_start
from test_route import at, route, why

from delegate.adapters.harness import opencode_sandbox as box
from delegate.app import env, status
from delegate.contracts.state import UsageCache
from delegate.domain.lanes import leases
from delegate.domain.lanes.lane import Lane


class RunCommand(RunBase):
    def test_no_timeout_unless_the_caller_passes_one(self):
        code, r = self.run_it(timeout=None)
        self.assertEqual((code, r["stop"]), (0, None))

    def test_a_codex_run_reads_the_codex_usage_before_the_launch_only(self):
        """No second read after the run: the codex app-server spawn cost 1-2 s per run (delegate-2ys)."""
        from delegate.app import usage
        calls = []
        usage.READERS["codex_app_server"] = lambda lane, S, st_, t, *a: calls.append(1)
        self.run_it()
        after_run = len(calls)
        calls.clear()
        route.main(["next", "worker"])
        self.assertEqual(after_run, len(calls))

    def test_unattended_keeps_the_orchestrator_reserve(self):
        support.put_out("OPENAI_SUBSCRIPTION", env.now() + 3600)
        f = Path(self.tmp.name) / ".claude" / "state" / "max-limits.json"
        d = json.loads(f.read_text())
        d["rate_limits"]["seven_day"]["used_percentage"] = 93                # above the 90% weekly reserve
        f.write_text(json.dumps(d))
        _, r = self.run_it()
        self.assertEqual(r["stop"], None)                               # the relaxed pass (ceiling 95) may still use it
        d["rate_limits"]["seven_day"]["used_percentage"] = 97
        f.write_text(json.dumps(d))
        _, r = self.run_it()
        self.assertEqual(why(r), "no_lane")

    def test_interactive_session_gets_the_agent_step_and_the_consent_question(self):
        self.codex_out()                                                    # Codex plans first, Claude only when it is down
        _, r = self.run_it(role="planner", unattended=False)
        self.assertEqual((r["stop"], r["lane"], r["agent_model"]), (None, "CLAUDE_SUBSCRIPTION", "sonnet"))
        self.assertIn("coordinator", r["brief_prefix"])
        [support.put_out(n, env.now() + 3600) for n in ("OPENAI_SUBSCRIPTION", "CLAUDE_SUBSCRIPTION")]
        _, r = self.run_it(role="planner", unattended=False)
        self.assertEqual(why(r), "no_lane")
        self.assertIn("ask_user", r)

    def test_a_busy_first_choice_waits_unless_told_not_to(self):
        for i in range(4):
            self.lease(f"r{i}", "OPENAI_SUBSCRIPTION")              # Codex, the worker's first choice, has 4 of 4 slots taken
        _, r = self.run_it()
        self.assertEqual((why(r), r["stop"]["permanence"], r["lane"]), ("busy", "transient", "OPENAI_SUBSCRIPTION"))
        self.assertTrue(time.time() + 50 < at(r) < time.time() + 130)

    def test_the_first_choice_is_rung_0_and_no_fallback(self):
        _, r = self.run_it()
        self.assertEqual((r["lane"], r["rung"], r["fallback"], r["fallback_reason"], r["first_lane"]), ("OPENAI_SUBSCRIPTION", 0, False, None, None))
        _, r = self.run_it("--resume", "thread-123")
        self.assertEqual((r["rung"], r["fallback"]), (0, False))
        self.codex_out()
        _, r = self.run_it()
        self.assertEqual((r["lane"], r["fallback"], r["fallback_reason"]), ("CLAUDE_SUBSCRIPTION", True, "out"))

    def test_three_running_leave_room_for_a_fourth(self):
        for i in range(3):
            self.lease(f"r{i}", "OPENAI_SUBSCRIPTION")
        _, r = self.run_it()
        self.assertEqual(r["lane"], "OPENAI_SUBSCRIPTION")

    def test_expired_leases_do_not_count(self):
        for i in range(4):
            self.lease(f"r{i}", "OPENAI_SUBSCRIPTION", expires=env.now() - 1)
        _, r = self.run_it()
        self.assertEqual(r["lane"], "OPENAI_SUBSCRIPTION")

    def test_a_strained_lane_allows_one(self):
        f = Path(self.tmp.name) / ".claude" / "state" / "max-limits.json"
        d = json.loads(f.read_text())
        d["rate_limits"]["five_hour"]["used_percentage"] = 85
        f.write_text(json.dumps(d))
        support.put_out("OPENAI_SUBSCRIPTION", env.now() + 3600)
        self.lease("c0", "CLAUDE_SUBSCRIPTION")
        _, r = self.run_it()
        self.assertEqual(why(r), "no_lane")          # one slot, taken: the first choice is out, so nothing waits for a slot

    def test_the_same_task_never_runs_twice_at_once(self):
        self.lease("r0", "OPENAI_SUBSCRIPTION", task="T-1")
        _, r = self.run_it("--task", "T-1")
        self.assertEqual(why(r), "duplicate_task")
        self.assertIn("T-1", r["say"])
        _, r = self.run_it("--task", "T-2")
        self.assertEqual(r["stop"], None)
        env.update_state(lambda s: s.leases.pop("r0"))
        _, r = self.run_it("--task", "T-1")             # a resume after the first run ended is fine
        self.assertEqual(r["stop"], None)

    def test_a_final_agent_message_alone_ends_the_run_after_the_grace(self):
        """Rollout 01a177fc: the final message arrived and turn.completed never did."""
        self.fast_clock()
        t0 = time.time()
        _, r = self.run_it(mode="message_only", timeout=0)
        self.assertLess(time.time() - t0, 40)
        self.assertEqual((r["stop"], r["text"], r["warnings"]), (None, "final, no turn.completed", ["answer_done_process_alive"]))
        self.pid_dead()

    def test_a_running_command_keeps_the_long_silence_limit(self):
        self.fast_clock()
        _, r = self.run_it(mode="tool_open", timeout=4)
        self.assertEqual(why(r), "timeout")

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_read_only_shell_may_write_dot_task_but_nothing_else_in_cwd(self):
        import subprocess as sp
        repo = Path(self.repo)
        sh = self.base / "sh-task"
        box.write_shell(self.repo, False, sh)
        run = lambda c: sp.run([str(sh), "-c", c], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(run("mkdir -p .task && touch .task/tmp1").returncode, 0)
        self.assertTrue((repo / ".task" / "tmp1").exists())
        self.assertNotEqual(run("touch made").returncode, 0)
        self.assertNotEqual(run("touch .taskx").returncode, 0)
        self.assertNotEqual(run("cat ~/.ssh/id_rsa").returncode, 0)

    def test_external_directory_lets_read_only_lanes_read_runs_and_temp_only(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/x/cfg"}):
            ext = box.permissions(False)["external_directory"]
        self.assertEqual(ext["*"], "deny")
        for want in ("/x/cfg/task-loop/runs/**", "/private/tmp/**", "/private/var/folders/**", "/tmp/**", "/var/folders/**"):
            self.assertEqual(ext[want], "allow")
        self.assertEqual(len(ext), 6)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertIn(str(Path.home() / ".config" / "task-loop" / "runs") + "/**", box.permissions(False)["external_directory"])
        self.assertEqual(box.permissions(True)["external_directory"], "deny")
        self.assertEqual(box.permissions(False)["read"][str(Path.home() / ".ssh")], "deny")

    def test_a_second_run_of_the_same_task_is_a_retry(self):
        self.lease("run-9", "OPENAI_SUBSCRIPTION", task="T-1")
        _, r = self.run_it("--task", "T-1")
        self.assertEqual((why(r), r["stop"]["permanence"]), ("duplicate_task", "transient"))

    def test_status_json_v1_shape(self):
        import io
        self.lease("run-1", "OPENAI_SUBSCRIPTION", task="T-5")
        self.run_it()
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            route.main(["status", "--json"])
            self.assertEqual(sys.stdout.getvalue().count("\n"), 1)
            out = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = real
        self.assertEqual(out["v"], 1)
        self.assertEqual([r["run_id"] for r in out["running"]], ["run-1"])
        self.assertEqual(out["running"][0]["task"], "T-5")
        self.assertNotIn("recent", out)
        self.assertIn("OPENAI_SUBSCRIPTION", out["lanes"])

    def test_timeout_kills_the_whole_process_group(self):
        code, r = self.run_it(mode="hang", timeout=2)
        self.assertEqual((code, why(r)), (0, "timeout"))
        pid = int(Path(os.environ["FAKE_PIDFILE"]).read_text())
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_status_json_has_a_health_row_per_pair_with_leases_and_last_failures(self):
        import io
        self.hist(self.failed_rows(3, 60))
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            route.main(["status", "--json"])
            payload = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = real
        row = next(x for x in payload["health"] if (x["lane"], x["model"]) == ("OPENAI_SUBSCRIPTION", "gpt-6.1-sol"))
        self.assertEqual((row["state"], row["source"], row["kind"], row["rung"]), ("broken", "breaker", "worker_failed", 1))
        self.assertEqual(row["leases"], {"live": 0, "suspect": 0})
        self.assertEqual(len(row["last_failures"]), 3)
        self.assertTrue(row["until"] > env.now() and row["evidence"] == "boom")
        other = next(x for x in payload["health"] if (x["lane"], x["model"]) == ("OPENAI_SUBSCRIPTION", "gpt-6-luna"))
        self.assertEqual(other["state"], "up")

    def test_a_suspect_lease_does_not_count_against_the_lane_cap(self):
        now = env.now()
        quiet = support.lease(lane="OPENAI_SUBSCRIPTION", role="worker", start=now - 3000, expires=now + 100, silence_s=2400, last_event=now - 2500)
        live = dataclasses.replace(quiet, last_event=now - 10)
        self.assertTrue(leases.suspect(quiet, now))
        self.assertFalse(leases.suspect(live, now))
        self.assertFalse(leases.suspect(support.lease(lane="x", start=now - 99999, expires=now + 1), now))     # an old lease without a limit is not guessed at
        st = support.doc_of({})
        st.leases = {"a": quiet, "b": live}
        self.assertEqual(leases.LeaseBook(st.leases).counts("OPENAI_SUBSCRIPTION", now), {"live": 2, "suspect": 1})
        self.assertEqual([r["suspect"] for r in status.running_rows(st, now)], [True, False])

    def test_git_changes_are_reported_not_failed(self):
        code, r = self.run_it(mode="git")
        self.assertEqual((code, r["stop"]), (0, None))
        self.assertTrue(any("branch" in x for x in r["git_violation"]), r["git_violation"])

    def test_empty_brief_is_an_error_object(self):
        code, r = self.run_it(brief="  ")
        self.assertEqual((code, why(r)), (0, "caller"))

    def test_an_orphaned_worker_of_a_dead_route_py_is_killed_before_the_task_runs_again(self):
        import subprocess as sp
        dead = sp.Popen(["true"]); dead.wait()
        orphan = sp.Popen(["sleep", "300"], start_new_session=True)
        try:
            start = proc_start(orphan.pid)
            support.put_lease("old-run", lane="OPENAI_SUBSCRIPTION", role="worker", pid=dead.pid, pid_start="Mon Jan  1 00:00:00 2001", start=env.now() - 300,
                              expires=env.now() + 60, task="T-7", worker_pid=orphan.pid, worker_start=start)
            _, r = self.run_it("--task", "T-7")
            self.assertEqual(r["stop"], None)                      # ran after the orphan was stopped
            self.assertIsNotNone(orphan.poll())
            rows = [json.loads(x) for x in env.history_path().read_text().splitlines()]
            self.assertIn((True, orphan.pid), [(x.get("reaped"), x.get("worker_pid")) for x in rows])
        finally:
            orphan.kill()

    def test_a_live_route_py_with_an_expired_lease_keeps_its_worker(self):
        import subprocess as sp
        worker = sp.Popen(["sleep", "300"], start_new_session=True)
        try:
            support.put_lease("old-run", lane="OPENAI_SUBSCRIPTION", role="worker", pid=os.getpid(), pid_start=proc_start(os.getpid()), start=env.now() - 300,
                              expires=env.now() - 1, task="T-9", worker_pid=worker.pid, worker_start=proc_start(worker.pid))
            _, r = self.run_it("--task", "T-9")
            self.assertEqual(why(r), "duplicate_task")
            self.assertIsNone(worker.poll())
        finally:
            worker.kill(); worker.wait()

    def test_a_reused_pid_is_not_killed(self):
        import subprocess as sp
        dead = sp.Popen(["true"]); dead.wait()
        other = sp.Popen(["sleep", "300"], start_new_session=True)
        try:
            support.put_lease("old-run", lane="OPENAI_SUBSCRIPTION", role="worker", pid=dead.pid, pid_start="x", start=env.now() - 300, expires=env.now() - 1,                 task="T-8", worker_pid=other.pid, worker_start="Mon Jan  1 00:00:00 2001")
            _, r = self.run_it("--task", "T-8")
            self.assertEqual(r["stop"], None)
            self.assertIsNone(other.poll())
        finally:
            other.kill(); other.wait()

    def test_a_resume_on_a_lane_with_all_slots_taken_waits_for_a_slot(self):
        self.run_it()
        for i in range(4):
            self.lease(f"run-{i}", "OPENAI_SUBSCRIPTION")
        _, r = self.run_it("--resume", "thread-123")
        self.assertEqual((why(r), r["stop"]["permanence"], r["lane"]), ("busy", "transient", "OPENAI_SUBSCRIPTION"))
        self.assertTrue(time.time() + 50 < at(r) < time.time() + 130)
        last = json.loads(env.history_path().read_text().splitlines()[-1])           # refusals are in history too
        self.assertEqual((last["outcome"], last["resume"], last["kind"]), ("busy", True, "refusal"))

    def test_a_plan_with_only_a_weekly_window_reads_it_as_the_week(self):
        res = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": env.now() + 3 * 86400},
                                                            "secondary": None, "rateLimitReachedType": None}}
        env.update_state(lambda s: setattr(s, "codex_limits", UsageCache(at=env.now(), data=res)))
        lane = Lane("OPENAI_SUBSCRIPTION", "openai", "subscription")
        self.real["codex_app_server"](lane, env.settings_of(env.load_routing()), env.load_state(), env.now())
        self.assertEqual((lane.five, lane.week), (None, 40.0))
        self.assertIsNotNone(lane.pace)

    def test_resume_continues_on_the_same_lane_and_never_switches(self):
        _, first = self.run_it()
        self.assertEqual(first["ref"], "thread-123")
        _, r = self.run_it("--resume", "thread-123")
        self.assertEqual((r["stop"], r["lane"]), (None, "OPENAI_SUBSCRIPTION"))
        self.assertIn("exec resume", self.args_seen())
        self.assertIn('sandbox_mode="workspace-write"', self.args_seen())
        support.put_out("OPENAI_SUBSCRIPTION", env.now() + 3600)
        n = len(self.args_seen().splitlines())
        _, r = self.run_it("--resume", "thread-123")
        self.assertEqual((why(r), r["lane"], r["stop"]["permanence"]), ("rate_limited", "OPENAI_SUBSCRIPTION", "transient"))
        self.assertGreater(at(r), time.time())
        self.assertIn("out until", r["say"])
        self.assertEqual(len(self.args_seen().splitlines()), n)            # nothing was launched
        _, r = self.run_it("--resume", "nope")
        self.assertEqual(why(r), "caller")

