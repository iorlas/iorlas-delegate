"""The supervisor's log watcher: a limit phrase in a log line that quotes the worker's own command must not mark the lane out; a real limit line must."""
import hermetic  # noqa: F401  (a home of its own for every test)
import tempfile
import unittest
from pathlib import Path

from test_route import route
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app.supervisor import LogWatcher

ECHO = ['INFO  2026-10-26T21:32:01 +3ms service=permission evaluated permission=bash pattern="cat <<EOF\\ntry again later\\nEOF" action=allow',
        'INFO  2026-10-26T21:32:02 +1ms service=permission evaluated permission=bash pattern="echo rate limit hit" action=allow']
REAL = 'ERROR 2026-10-26T21:40:00 +9ms service=llm error=Rate limit reached for requests, try again later'


class LogWatch(unittest.TestCase):
    def run_poll(self, lines):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.log").write_text("old\n")
            w = LogWatcher(Path(d), route.classify.LIMIT_RE.search, OpenCodeHarness().echoes_input)
            with open(Path(d) / "a.log", "a") as f:
                f.write("\n".join(lines) + "\n")
            return w.poll()

    def test_echoed_tool_input_is_ignored(self):
        self.assertIsNone(self.run_poll(ECHO))

    def test_real_limit_line_still_marks_out(self):
        stop = self.run_poll(ECHO + [REAL])
        self.assertEqual(stop.why, "limit")
        self.assertIn("Rate limit reached", stop.msg)

    def test_lines_from_before_the_launch_are_not_seen(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.log").write_text(REAL + "\n")
            self.assertIsNone(LogWatcher(Path(d), route.classify.LIMIT_RE.search, OpenCodeHarness().echoes_input).poll())


if __name__ == "__main__":
    unittest.main()
