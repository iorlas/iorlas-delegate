"""delegate-81g: a write step may not kill processes it did not start nor run the checks the brief leaves to the engine."""
import io
import json
import os
import sys
import unittest
from unittest import mock

import hermetic  # noqa: F401  (a home of its own for every test)
from test_route import route

from delegate.app import decide, launch
from delegate.cli import parser
from delegate.contracts import wire
from delegate.domain.policy.worker import judge

KILLS = ('pkill -f "Unity.*client"', "kill 4242 4243", "killall Unity", "cd /s && pkill -9 -f Unity; echo done", "kill -9 $(pgrep Unity)",
         "for p in 1 2; do kill $p; done")
ALLOWED = ("just test-fast tests/test_x.py", "python3 -m unittest discover -s tests", "uv run pytest -q", "ps aux | grep Unity",
           "pgrep -f Unity", "server & kill $!", "sleep 100 & kill %1", "git diff --stat", "just fmt-check")


def guard(command: str, *flags: str) -> tuple[int, str]:
    real = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})), io.StringIO()
    try:
        code = route.main(["guard", *flags])
        return code, sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stderr = real


class EngineOnlyRequestTest(unittest.TestCase):
    """The recipe list is a typed request field (--engine-only once per item), never an environment variable."""

    def flags(self, **kw):
        argv = wire.RunRequest(role="writer", brief_file="b", cwd="/w", **kw).to_argv("route.py")
        ns = parser.build().parse_args(argv[2:])
        cmd = json.loads(json.dumps(launch.guard_settings(False, tuple(ns.engine_only))))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        return argv, cmd

    def test_flag_per_item_reaches_the_guard(self):
        argv, cmd = self.flags(engine_only=["unity-tests", "e2e"])
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--engine-only"], ["unity-tests", "e2e"])
        self.assertIn("--engine-only=unity-tests", cmd)
        self.assertEqual(guard("just unity-tests", "--engine-only=unity-tests")[0], 2)
        self.assertEqual(guard("just test-fast", "--engine-only=unity-tests")[0], 0)

    def test_flag_absent_blocks_nothing_and_old_env_var_is_ignored(self):
        with mock.patch.dict(os.environ, {"DELEGATE_ENGINE_ONLY": "unity-tests"}):
            argv, cmd = self.flags()
        self.assertNotIn("--engine-only", argv)
        self.assertNotIn("engine-only", cmd)
        self.assertEqual(guard("just unity-tests")[0], 0)

    def test_advisory_request_reaches_the_router_and_keeps_supported_guards(self):
        argv, cmd = self.flags(engine_only=["unity-tests", "e2e"], engine_only_advisory=True)
        ns = parser.build().parse_args(argv[2:])
        args = decide.call_args(ns, unattended=True)
        self.assertTrue(args.engine_only_advisory)
        self.assertEqual(args.engine_only, ("unity-tests", "e2e"))
        self.assertIn("--engine-only=unity-tests", cmd)
        self.assertEqual(argv.count("--engine-only-advisory"), 1)
        default = wire.RunRequest(role="writer", brief_file="b", cwd="/w").to_argv("route.py")
        self.assertNotIn("--engine-only-advisory", default)


class WriterGuardTest(unittest.TestCase):
    def test_killing_processes_the_step_did_not_start_is_blocked_with_the_rule(self):
        for c in KILLS:
            code, why = guard(c)
            self.assertEqual(code, 2, c)
            self.assertIn("engine reruns the check", why)

    def test_ordinary_test_runs_and_killing_its_own_job_pass(self):
        for c in ALLOWED:
            self.assertEqual(guard(c)[0], 0, c)

    def test_engine_only_recipes_are_named_and_blocked(self):
        code, why = guard("cd client && just unity-tests", "--engine-only=unity-tests")
        self.assertEqual(code, 2)
        self.assertIn("just unity-tests", why)
        self.assertIn("engine reruns the check", why)
        self.assertEqual(guard("just test-fast", "--engine-only=unity-tests")[0], 0)

    def test_judge_leaves_read_only_steps_to_their_own_rule(self):
        self.assertTrue(judge("Bash", {"command": "pkill x"}, False).blocked)
        self.assertIn("not a reading command", judge("Bash", {"command": "pkill x"}, True).reason)


if __name__ == "__main__":
    unittest.main()
