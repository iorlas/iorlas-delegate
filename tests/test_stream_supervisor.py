"""delegate-np7: one supervisor for Codex, Claude Code and opencode. Fixture replays: a fake CLI prints a recorded stream and never exits (or stalls);
no real model is called. Grace and stall are cut to ~1 s through the supervisor constants."""
import hermetic  # noqa: F401  (a home of its own for every test)
import argparse
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "delegate"))
import route                                  # noqa: E402
from lanes import supervisor, usage          # noqa: E402

T = 1_000_000.0


def j(**kw):
    return json.dumps(kw)


CLAUDE_TOOL = j(type="assistant", message={"id": "m1", "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 2},
                                           "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}]})
CLAUDE_TOOL_DONE = j(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "tu1"}]})
CLAUDE_FINAL = j(type="assistant", message={"id": "m2", "stop_reason": "end_turn", "usage": {"input_tokens": 5, "cache_read_input_tokens": 40, "output_tokens": 7},
                                            "content": [{"type": "text", "text": "the final words"}]})
CLAUDE_RESULT = j(type="result", result="claude answer", session_id="sess-1", is_error=False, num_turns=2, total_cost_usd=0.25,
                  usage={"input_tokens": 15, "cache_read_input_tokens": 40, "output_tokens": 9})
OC_STEP_TOOLS = j(type="step_finish", sessionID="ses_1", part={"reason": "tool-calls", "tokens": {"input": 10, "output": 1}})
OC_TEXT = j(type="text", sessionID="ses_1", part={"text": "opencode answer"})
OC_STEP_STOP = j(type="step_finish", sessionID="ses_1", part={"reason": "stop", "tokens": {"input": 20, "output": 4, "cache": {"read": 5, "write": 0}}})
CODEX_STARTED = j(type="thread.started", thread_id="th-1")
CODEX_MSG = j(type="item.completed", item={"id": "i1", "type": "agent_message", "text": "codex answer"})
CODEX_CMD = j(type="item.completed", item={"id": "i2", "type": "command_execution", "command": "ls", "status": "completed", "exit_code": 0})
CODEX_DONE = j(type="turn.completed", usage={"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7})


@unittest.skipIf(os.name == "nt", "fake CLIs are shell scripts")
class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pidfile = self.base / "pid"
        self.cfg = self.base / "cfg"
        self.cfg.mkdir()
        for k, v in (("ANSWER_GRACE_S", 1), ("STALL_S", 1)):
            self.addCleanup(setattr, supervisor, k, getattr(supervisor, k))
            setattr(supervisor, k, v)
        self.addCleanup(setattr, supervisor, "GRACE_OF", supervisor.GRACE_OF)
        supervisor.GRACE_OF = {}
        env = dict(os.environ, DELEGATE_CONFIG_DIR=str(self.cfg), HOME=str(self.base), CODEX_HOME=str(self.base))
        for p in (mock.patch.dict(os.environ, env), mock.patch.object(route, "agent_runs", None), mock.patch.object(route.sys, "platform", "darwin"),
                  mock.patch.object(route, "opencode_shell"), mock.patch.object(route, "child_env", side_effect=lambda extra=None: dict(os.environ, **(extra or {}))),
                  mock.patch.object(route, "codex_thread_total", return_value=None), mock.patch.object(route, "codex_child_tokens", return_value=({"in": 0, "cached": 0, "out": 0}, 0)),
                  mock.patch.object(route, "codex_context_k", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.a = argparse.Namespace(resume=None, schema=None, timeout=0, role="worker")

    def fake(self, name, lines, hang=True):
        """A script that prints `lines` and then never exits (its sleep child's pid goes to self.pidfile)."""
        body = "#!/bin/sh\n[ \"$1\" = export ] && exit 1\ncat > /dev/null\n"
        emit = "".join(f"echo '{x}'\n" for x in lines)
        body += emit + (f"sleep 300 & echo $! > {self.pidfile}; wait\n" if hang else "")
        f = self.base / name
        f.write_text(body)
        f.chmod(0o755)
        return str(f)

    def alive(self):
        time.sleep(0.3)
        try:
            os.kill(int(self.pidfile.read_text()), 0)
            return True
        except OSError:
            return False

    def d(self, kind):
        lane = {"codex": "OPENAI_SUBSCRIPTION", "claude": "CLAUDE_SUBSCRIPTION", "opencode": "GROK_SUBSCRIPTION"}[kind]
        how = dict(model="m", cwd=str(self.base), write=False, read_only=False, cap=1, sandbox="read-only", cli=["codex", "exec"], isolation=[])
        return dict(lane=lane, model="m", effort="high", role="worker", how=how)

    def launch(self, kind, exe, a=None):
        a = a or self.a
        d = self.d(kind)
        with mock.patch.object(route.shutil, "which", return_value=exe), mock.patch.object(route, "opencode_exe", return_value=exe):
            if kind == "codex":
                return route.launch_codex(d, "brief", a, "rid12345", T, {})
            if kind == "claude":
                return route.launch_claude(d, "brief", a, "rid12345", T, route.load_routing(), {})
            return route.launch_opencode(d, "brief", a, "rid12345", T, {})

    CASES = {"codex": ([CODEX_STARTED, CODEX_CMD, CODEX_MSG, CODEX_DONE], "codex answer"),
             "claude": ([CLAUDE_TOOL, CLAUDE_TOOL_DONE, CLAUDE_FINAL, CLAUDE_RESULT], "claude answer"),
             "opencode": ([OC_STEP_TOOLS, OC_TEXT, OC_STEP_STOP], "opencode answer")}

    def test_a_stream_ending_in_its_completion_event_with_a_process_that_never_exits_is_done_in_under_40_s(self):
        for kind, (lines, text) in self.CASES.items():
            with self.subTest(kind):
                t0 = time.time()
                r = self.launch(kind, self.fake(kind, lines))
                self.assertLess(time.time() - t0, 40)
                self.assertEqual((r.get("outcome"), r["text"]), (None, text), r)
                self.assertTrue(r["stream_tail"] and lines[-1] in r["stream_tail"][-1], r["stream_tail"])      # the tail is kept on any kill
                self.assertFalse(self.alive())
                if kind != "claude":
                    self.assertEqual(r["warnings"], ["answer_done_process_alive"])

    def test_a_final_message_without_the_completion_event_ends_the_run_too(self):
        r = self.launch("claude", self.fake("claude", [CLAUDE_TOOL, CLAUDE_TOOL_DONE, CLAUDE_FINAL]))
        self.assertEqual((r.get("outcome"), r["text"], r["warnings"]), (None, "the final words", ["answer_done_process_alive"]))
        self.assertEqual(r["tokens"], {"in": 55, "cached": 40, "out": 9})          # no result event: usage folded from the stream's requests
        self.assertEqual((r["acct"]["in_fresh"], r["acct"]["cache_read"], r["acct"]["out"]), (15, 40, 9))
        r = self.launch("codex", self.fake("codex", [CODEX_STARTED, CODEX_MSG]))
        self.assertEqual((r.get("outcome"), r["text"]), (None, "codex answer"))
        self.assertFalse(self.alive())

    def test_a_later_event_resets_the_grace(self):
        script = self.base / "slowclaude"
        script.write_text(f"#!/bin/sh\ncat >/dev/null\necho '{CLAUDE_RESULT}'\nsleep 0.7\necho '{CLAUDE_RESULT}'\nsleep 300 & echo $! > {self.pidfile}; wait\n")
        script.chmod(0o755)
        t0 = time.time()
        r = self.launch("claude", str(script))
        self.assertGreaterEqual(time.time() - t0, 1.5)      # the second turn's event at 0.7 s restarted the 1 s grace
        self.assertEqual((r.get("outcome"), r["text"]), (None, "claude answer"))

    def test_a_stalled_stream_is_killed_and_its_tail_logged(self):
        stalls = {"codex": [CODEX_STARTED, CODEX_CMD], "claude": [CLAUDE_TOOL, CLAUDE_TOOL_DONE], "opencode": [OC_STEP_TOOLS]}
        for kind, lines in stalls.items():
            with self.subTest(kind):
                t0 = time.time()
                r = self.launch(kind, self.fake(kind, lines))
                self.assertLess(time.time() - t0, 40)
                self.assertEqual(r["outcome"], "hung", r)
                self.assertEqual(r["stream_tail"][-1], lines[-1])
                self.assertFalse(self.alive())

    def test_a_tool_in_flight_keeps_the_long_silence_limit(self):
        for kind, lines in (("codex", [CODEX_STARTED, j(type="item.started", item={"id": "i1", "type": "command_execution", "command": "make"})]),
                            ("claude", [CLAUDE_TOOL])):
            with self.subTest(kind):
                self.a.timeout = 4
                r = self.launch(kind, self.fake(kind, lines))
                self.assertEqual(r["outcome"], "timeout")
                self.assertTrue(r["stream_tail"])
                self.assertFalse(self.alive())

    def test_turn_rules_with_an_injected_clock(self):
        now = [0.0]
        wd = supervisor.Watchdog(120, 600, clock=lambda: now[0])
        turn = supervisor.Turn("codex", clock=lambda: now[0])
        wd.touch()
        turn.finished()
        now[0] = 0.5
        wd.poll()
        self.assertIsNone(turn.verdict(wd))
        turn.more()
        turn.finished()
        now[0] = 1.6
        self.assertEqual(turn.verdict(wd), "answer")

    def test_claude_stream_usage_sums_requests(self):
        self.assertEqual(usage.claude_stream([{"input_tokens": 1, "output_tokens": 2}, {"input_tokens": 3, "cache_read_input_tokens": 9}]),
                         {"input_tokens": 4, "cache_read_input_tokens": 9, "cache_creation_input_tokens": 0, "output_tokens": 2})
        self.assertEqual(usage.claude_stream([]), {})


if __name__ == "__main__":
    unittest.main()
