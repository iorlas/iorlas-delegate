"""`route.py run` stops: network drops, canary and bench, schema re-ask, the answer's stop facts and contract."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (a home of its own for every test)
import routing_support as support
from run_support import RunBase
from test_route import CFG, at, route, why

from delegate.adapters.harness import reset_parse
from delegate.app import answer, env, launch, net, usage, watch
from delegate.app import decide as dec
from delegate.contracts import history as hist
from delegate.contracts import stops
from delegate.domain.health import ledger as health_ledger


class RunStops(RunBase):
    def test_unattended_never_answers_agent_ask_user_or_needs_input(self):
        [support.put_out(n, env.now() + 3600) for n in ("OPENAI_SUBSCRIPTION", "CLAUDE_SUBSCRIPTION")]
        code, r = self.run_it(role="planner")
        self.assertEqual((code, why(r)), (0, "no_lane"))
        self.assertNotIn("ask_user", r)
        self.assertNotIn("needs_input", r)
        support.clear_out()
        self.codex_out()
        _, r = self.run_it(role="planner")                                 # no Agent tool here: the subscription runs claude -p (Sonnet; Opus never plans in the loop)
        self.assertIsNone(r.get("agent_model"))
        self.assertEqual((r["stop"], r["lane"]), (None, "CLAUDE_SUBSCRIPTION"))

    def test_every_lane_full_stops_with_a_retry_after_in_one_to_two_minutes(self):
        for i in range(4):
            self.lease(f"o{i}", "OPENAI_SUBSCRIPTION")
            self.lease(f"c{i}", "CLAUDE_SUBSCRIPTION")
        _, r = self.run_it()
        self.assertEqual(why(r), "busy")
        self.assertTrue(55 <= at(r) - time.time() <= 125, at(r))

    def test_the_lease_is_released_and_a_history_line_is_written(self):
        _, r = self.run_it("--task", "T-9")
        self.assertEqual(env.load_state().leases, {})
        line = json.loads(env.history_path().read_text().splitlines()[-1])
        self.assertEqual((line["run_id"], line["task"], line["lane"], line["done"]), (r["run_id"], "T-9", "OPENAI_SUBSCRIPTION", True))
        self.assertEqual(line["tokens"], {"in": 100, "cached": 40, "out": 7})
        self.assertNotIn("brief", json.dumps(line))
        self.assertEqual(line["overlap"], [])

    def test_answer_done_process_alive_is_done_after_the_grace(self):
        """delegate-itn: turn.completed after the final agent message, then the process never exits: done after the grace, whole tree stopped."""
        from delegate.app import watch
        self.addCleanup(setattr, watch, "ANSWER_GRACE_S", watch.ANSWER_GRACE_S)
        watch.ANSWER_GRACE_S = 1
        t0 = time.time()
        _, r = self.run_it(mode="answer_alive", timeout=0)
        self.assertLess(time.time() - t0, 40)
        self.assertEqual((r["stop"], r["text"], r["lane"]), (None, '{"findings": []}', "OPENAI_SUBSCRIPTION"))
        self.assertEqual(r["warnings"], ["answer_done_process_alive"])
        self.assertIsNone(r["stop"])
        self.assertEqual(json.loads(env.history_path().read_text().splitlines()[-1])["warnings"], ["answer_done_process_alive"])
        self.assertNotIn("OPENAI_SUBSCRIPTION", env.load_state().out)
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(int(Path(os.environ["FAKE_PIDFILE"]).read_text()), 0)

    def test_a_later_event_resets_the_answer_grace(self):
        self.fast_clock(grace=1, stall=900)
        _, r = self.run_it(mode="progress_then_work", timeout=4)
        self.assertEqual(why(r), "timeout")

    def test_a_stalled_model_request_is_stopped_and_the_stream_tail_is_logged(self):
        self.fast_clock()
        _, r = self.run_it(mode="stall", timeout=20)
        self.assertEqual((why(r), r["stop"]["scope"]), ("hung", "model"))
        row = json.loads(env.history_path().read_text().splitlines()[-1])
        self.assertTrue(any('"command_execution"' in x for x in row["stream_tail"]))
        self.pid_dead()

    def test_sigterm_on_route_py_stops_the_worker_too(self):
        import signal
        penv = dict(os.environ, FAKE_MODE="hang", PATH=str(self.base / "bin") + os.pathsep + os.environ["PATH"])
        p = subprocess.Popen([sys.executable, route.__file__, "run", "worker", "--cwd", self.repo, "--unattended"], env=penv,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(b"do it"); p.stdin.close()
        for _ in range(100):
            if Path(os.environ["FAKE_PIDFILE"]).exists() and Path(os.environ["FAKE_PIDFILE"]).read_text().strip():
                break
            if p.poll() is not None:
                self.fail(p.stdout.read().decode() + p.stderr.read().decode())
            time.sleep(0.1)
        pid = int(Path(os.environ["FAKE_PIDFILE"]).read_text())
        wp = [v.worker_pid for v in env.load_state().leases.values()]
        self.assertTrue(wp and wp[0], "the lease names the worker before it does any work")
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        self.assertIn(f"delegate: worker pid={wp[0]} pgid={wp[0]}", p.stderr.read().decode())
        time.sleep(0.3)
        with self.assertRaises(OSError):
            os.kill(pid, 0)
        self.assertEqual(env.load_state().leases, {})

    def test_a_state_lock_timeout_is_a_stop_with_a_retry_time(self):
        import io
        from unittest import mock
        with mock.patch.object(dec, "decision", side_effect=TimeoutError("state is locked")):
            real, sys.stdout = sys.stdout, io.StringIO()
            try:
                route.main(["run", "worker", "--unattended"])
                out = json.loads(sys.stdout.getvalue())
            finally:
                sys.stdout = real
        self.assertEqual((why(out), out["stop"]["permanence"]), ("lock", "transient"))
        self.assertGreater(at(out), time.time())

    def test_a_resume_with_no_rollout_subtracts_the_cumulative_the_history_kept(self):
        self.run_it()       # first call: the thread total is 100/40/7, recorded in history
        env.history_add(hist.RunRow(ts=env.now(), run_id="kept", role="worker", lane="OPENAI_SUBSCRIPTION", model="m", effort="high", done=True,
                                             ref="thread-123", cumulative={"in": 60, "cached": 30, "out": 4}))
        _, r = self.run_it("--resume", "thread-123")
        self.assertEqual(r["tokens"], {"in": 40, "cached": 10, "out": 3})
        self.assertEqual((r["usage"]["tokens"]["in_fresh"], r["usage"]["tokens"]["cache_read"], r["usage"]["tokens"]["out"]), (30, 10, 3))

    def test_every_answer_carries_the_stop_facts(self):
        cases = {"early_error": ("vendor_blip", "lane", "transient"), "bad_request": ("bad_request", "caller", "needs_owner"),
                 "late_error": ("network", "machine", "transient"), "secret": ("secrets", "bead", "needs_owner"),
                 "logout": ("logged_out", "lane", "needs_owner"), "net": ("network", "machine", "transient"), "ok": None}
        for mode, want in cases.items():
            _, r = self.run_it(mode=mode)
            self.assertEqual(r["contract"], 3)
            self.assertEqual(tuple(r["stop"][k] for k in ("reason", "scope", "permanence")) if r["stop"] else None, want, mode)
            for gone in ("v", "status", "reason", "next", "retry_after"):
                self.assertNotIn(gone, r, mode)
            if want and want[2] == "transient":
                self.assertGreater(at(r), time.time())
        self.assertEqual(r["context"]["instructions"], [])
        self.assertIn("PATH", r["context"]["env_keys"])
        self.assertNotIn("CLAUDE_FOO", r["context"]["env_keys"])

    def test_a_drop_before_the_first_event_relaunches_once(self):
        _, r = self.run_it(mode="drop_early")
        self.assertEqual((r["stop"], r["text"]), (None, "second try"))
        self.assertEqual(self.launches(), 2)

    def test_two_drops_before_the_first_event_stop_with_network_and_launch_no_third_time(self):
        _, r = self.run_it(mode="drop_early_twice")
        self.assertEqual((why(r), r["stop"]["scope"]), ("network", "machine"))
        self.assertEqual(self.launches(), 2)
        self.assertFalse(r["stop"]["resumable"])
        self.assertFalse(r["stop"]["partial"])
        self.assertNotIn("OPENAI_SUBSCRIPTION", env.load_state().out)      # the network is not the lane's fault

    def test_a_drop_with_the_net_still_down_is_not_relaunched_in_the_call(self):
        with mock.patch.object(net, "net_probe", lambda hosts=None: {"up": False, "ip_up": False, "why": "offline", "ms": 1, "hosts": {}, "at": time.time()}):
            _, r = self.run_it(mode="drop_early")
        self.assertEqual((why(r), r["stop"]["scope"]), ("network", "machine"))
        self.assertEqual(self.launches(), 1)         # no in-call wait or retry loop: the engine decides

    def test_a_dropped_stream_keeps_the_ref_in_the_answer_the_stop_and_the_refs_table(self):
        _, r = self.run_it(mode="late_error")
        self.assertEqual((r["ref"], r["stop"]["ref"]), ("thread-123", "thread-123"))
        self.assertIn("thread-123", env.load_state().refs)         # a later --resume finds the lane that made it
        row = json.loads(Path(env.history_path()).read_text().splitlines()[-1])
        self.assertEqual((row["outcome"], row["ref"]), ("network", "thread-123"))

    def test_every_stop_that_has_a_ref_carries_it(self):
        _, r = self.run_it(mode="limit")
        self.assertEqual((why(r), r["ref"]), ("rate_limited", "thread-123"))
        self.assertEqual(r["stop"]["ref"], "thread-123")

    def test_network_stops_never_bench_the_pair(self):
        for _ in range(5):
            _, r = self.run_it(mode="late_error", unattended=True)
            self.assertEqual(why(r), "network")
        led = health_ledger.HealthLedger(env.typed_routing(CFG).settings.breaker)
        st = led.fold(env.history_store().rows())
        self.assertTrue(led.view(st, "OPENAI_SUBSCRIPTION", r["model"], time.time()).usable)
        self.assertEqual(st.pairs, {})
        _, r = self.run_it()
        self.assertEqual(r["stop"], None)

    def test_a_stop_with_nothing_to_wait_for_is_for_the_owner(self):
        self.assertEqual(answer.result("no_lane")["stop"]["permanence"], "needs_owner")
        self.assertEqual(answer.result("no_lane", retry_after=5)["stop"]["permanence"], "transient")
        self.assertIsNone(answer.result()["stop"])

    def test_schema_is_reasked_once_in_the_same_thread(self):
        schema = self.base / "s.json"
        schema.write_text('{"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}')
        _, r = self.run_it("--schema", str(schema), mode="badjson")
        self.assertEqual((r["stop"], r["data"]), (None, {"a": 1}))
        self.assertIn("resume", self.args_seen().splitlines()[-1])
        self.assertEqual(r["tokens"]["in"], 200)
        _, r = self.run_it("--schema", str(schema), mode="badjson_always")
        self.assertEqual((why(r), r["stop"]["permanence"]), ("schema_invalid", "transient"))
        self.assertIn("$.a is missing", r["say"])

    def test_contract_json_names_every_reason_and_lane(self):
        import io
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            route.main(["contract", "--json"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdout = real
        self.assertEqual(out.count("\n"), 1)
        c = json.loads(out)
        self.assertEqual(c["contract"], 3)
        self.assertEqual(set(c["reasons"]), set(stops.CONTRACT["reasons"]))
        for row in c["reasons"].values():
            self.assertTrue({"scope", "permanence", "retry", "health", "wording"} <= set(row))

    def test_a_request_fault_leaves_the_lane_in_state_json_alone(self):
        before = env.load_state().out
        self.run_it(mode="bad_request")
        self.assertEqual(env.load_state().out, before)

    def test_handshake_declares_contract_3_and_the_reason_table(self):
        import io
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            route.main(["contract", "--json"])
            c = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = real
        self.assertEqual(c["contract"], 3)
        self.assertEqual(set(c), {"contract", "reasons", "run_args"})
        self.assertIn("--call-id", c["run_args"])
        self.assertNotIn("--host", c["run_args"])
        self.assertEqual(set(c["reasons"]), set(stops.CONTRACT["reasons"]))

    def test_history_rotates_by_age_once_it_is_big(self):
        p = env.history_path()
        old = json.dumps({"ts": time.time() - 20 * 86400, "run_id": "old", "pad": "x" * 2000})
        p.write_text((old + "\n") * 600)                                      # about 1.2 MB, all older than 14 days
        self.run_it()
        runs = [json.loads(x)["run_id"] for x in p.read_text().splitlines()]
        self.assertNotIn("old", runs)
        self.assertEqual(len(runs), 1)

    def test_a_gap_over_1800s_is_written_to_history_through_the_watchdog_and_the_fold_ignores_it(self):
        from delegate.app import watch
        t = [0.0]
        d = {"lane": "OPENAI_SUBSCRIPTION", "model": "gpt-6.1-sol"}
        dog = watch.Watchdog(120, 3600, clock=lambda: t[0])
        for at in (5, 1805, 4005):       # gaps of 1800 s (not logged) and 2200 s (logged)
            t[0] = at
            dog.touch()
        self.assertFalse(dog.poll())
        dog.flush(env.history_add, d["lane"], d["model"], True)
        rows = env.history_store().rows()
        self.assertEqual([(type(r).__name__, r.lane, r.model, r.gap_s, r.finished) for r in rows], [("GapRow", "OPENAI_SUBSCRIPTION", "gpt-6.1-sol", 2200.0, True)])
        self.assertEqual(health_ledger.HealthLedger(env.typed_routing(CFG).settings.breaker).fold(rows).pairs, {})
        dog.flush(None, d["lane"], d["model"], True)       # never raises

    def test_any_stream_line_counts_as_activity_even_on_stderr(self):
        with mock.patch.dict(watch.LIMITS["codex"], silence_s=1):      # the fake prints to stderr every 0.4 s for 3 s
            _, r = self.run_it(mode="chatter", timeout=0)
        self.assertEqual(r["stop"], None)

    def test_a_benched_pair_is_skipped_and_the_next_step_runs(self):
        self.hist(self.failed_rows(3, 60))
        _, r = self.run_it(unattended=True)
        self.assertNotEqual(r.get("model"), "gpt-6.1-sol")
        self.assertNotEqual(r.get("lane"), "OPENAI_SUBSCRIPTION")

    def test_a_pair_whose_bench_ended_gets_one_canary_before_the_real_call(self):
        self.hist(self.failed_rows(3, 1500))            # benched 15 min from the third failure: over 8 min ago
        _, r = self.run_it()
        self.assertEqual((r["stop"], r["lane"]), (None, "OPENAI_SUBSCRIPTION"))
        calls = self.args_seen().strip().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertEqual(Path(os.environ["FAKE_ARGS"] + ".brief").read_text().strip().splitlines()[-1], "the brief")
        rows = [json.loads(x) for x in env.history_path().read_text().splitlines()]
        self.assertEqual([x["kind"] for x in rows[-2:]], ["canary", "run"])
        _, r = self.run_it()                           # the canary passed: the ladder is reset, the next call has none
        self.assertEqual(len(self.args_seen().strip().splitlines()), 3)

    def test_a_failed_canary_is_the_answer_and_starts_the_next_rung(self):
        self.hist(self.failed_rows(3, 1500))
        _, r = self.run_it(mode="early_error")
        self.assertEqual(why(r), "vendor_blip")
        self.assertTrue(r["say"].startswith("canary failed: "), r["say"])
        self.assertEqual(len(self.args_seen().strip().splitlines()), 1)         # the real brief was never sent
        led = health_ledger.HealthLedger(env.typed_routing(CFG).settings.breaker)
        v = led.view(led.fold(env.history_store().rows()), "OPENAI_SUBSCRIPTION", "gpt-6.1-sol", env.now() + 1)
        self.assertEqual((v.state, v.rung), ("broken", 2))
        self.assertAlmostEqual(v.until - env.now(), 30 * 60, delta=5)

    def test_bad_request_never_benches_the_pair(self):
        for _ in range(4):
            _, r = self.run_it(mode="bad_request")
            self.assertEqual((why(r), r["stop"]["permanence"]), ("bad_request", "needs_owner"))
        _, r = self.run_it()
        self.assertEqual((r["stop"], r["lane"], r["model"]), (None, "OPENAI_SUBSCRIPTION", "gpt-6.1-sol"))

    def test_workers_of_a_lane_marked_out_by_someone_else_are_stopped_within_the_poll_interval(self):
        from delegate.app.supervisor import LaneGoneWatcher
        started = time.time()
        w = LaneGoneWatcher(launch.lane_gone_check("OPENAI_SUBSCRIPTION", started), every=0)
        self.assertIsNone(w.poll())
        launch.settler().mark_out("OPENAI_SUBSCRIPTION", env.now() + 3600, "limit refusal: usage limit", env.now())
        stop = w.poll()
        self.assertEqual(stop.why, "limit")
        late = LaneGoneWatcher(launch.lane_gone_check("OPENAI_SUBSCRIPTION", time.time() + 5), every=0)     # a worker that started after the mark is not affected
        self.assertIsNone(late.poll())

    def test_schema_is_passed_and_the_answer_parsed(self):
        schema = self.base / "s.json"
        schema.write_text('{"type": "object"}')
        _, r = self.run_it("--schema", str(schema), mode="json")
        self.assertEqual((r["stop"], r["data"]), (None, {"a": 1}))
        self.assertIn("--output-schema", self.args_seen())

    def test_a_limit_refusal_lasts_until_the_known_reset(self):
        soon = env.now() + 500

        def reading(lane, S, st, t, *a):
            if lane.family == "openai":
                lane.five, lane.five_reset, lane.week, lane.week_reset = 50, soon, 10, soon + 99999
        usage.READERS["codex_app_server"] = reading
        self.run_it(mode="limit")
        self.assertAlmostEqual(env.load_state().out["OPENAI_SUBSCRIPTION"].until, soon, delta=1)
        when, source, _ = reset_parse.limit_until("You've hit your usage limit. ... or try again at 5:50 AM.", env.now(), {})
        self.assertEqual(time.strftime("%H:%M", time.localtime(when)), "05:50")
        self.assertTrue(env.now() < when <= env.now() + 86400)
        self.assertEqual(source, "vendor")
        when, source, _ = reset_parse.limit_until("the batch run is queued behind a rate limit", env.now(), {})
        self.assertEqual((source, round(when - env.now())), ("guess", 900))       # nobody said when: refusal time + 15 min

    def test_a_reading_with_room_after_the_refusal_clears_the_out(self):
        support.put_out("OPENAI_SUBSCRIPTION", env.now() + 5 * 86400, "limit refusal: x", at=env.now() - 60)

        def reading(lane, S, st, t, *a):
            lane.five, lane.week, lane.read_at = 0, 0, t
        usage.READERS["codex_app_server"] = reading
        _, r = self.run_it()
        self.assertEqual((r["stop"], r["lane"]), (None, "OPENAI_SUBSCRIPTION"))
        self.assertNotIn("OPENAI_SUBSCRIPTION", env.load_state().out)

    def test_the_model_saying_rate_limit_is_not_a_limit_refusal(self):
        usage.READERS["codex_app_server"] = lambda lane, S, st, t, *a: None
        _, r = self.run_it(mode="saylimit")
        self.assertNotIn("OPENAI_SUBSCRIPTION", env.load_state().out)
        self.assertNotEqual(why(r), "rate_limited")

