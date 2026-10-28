"""delegate-81g: a write step may not kill processes it did not start nor run the checks the brief leaves to the engine."""
import io
import json
import sys
import unittest

import hermetic  # noqa: F401  (a home of its own for every test)
from test_route import route

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
