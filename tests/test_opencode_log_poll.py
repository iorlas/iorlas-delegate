"""opencode_log_poll: a limit phrase in a log line that quotes the worker's own command must not mark the lane out; a real limit line must."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "delegate"))
import route  # noqa: E402

ECHO = ['INFO  2026-10-26T21:32:01 +3ms service=permission evaluated permission=bash pattern="cat <<EOF\\ntry again later\\nEOF" action=allow',
        'INFO  2026-10-26T21:32:02 +1ms service=permission evaluated permission=bash pattern="echo rate limit hit" action=allow']
REAL = 'ERROR 2026-10-26T21:40:00 +9ms service=llm error=Rate limit reached for requests, try again later'


class OpencodeLogPoll(unittest.TestCase):
    def run_poll(self, lines):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(route, "opencode_log_dir", return_value=Path(d)):
                (Path(d) / "a.log").write_text("old\n")
                poll, state = route.opencode_log_poll()
                with open(Path(d) / "a.log", "a") as f:
                    f.write("\n".join(lines) + "\n")
                return poll(), state

    def test_echoed_tool_input_is_ignored(self):
        hit, state = self.run_poll(ECHO)
        self.assertFalse(hit)
        self.assertIsNone(state["why"])

    def test_real_limit_line_still_marks_out(self):
        hit, state = self.run_poll(ECHO + [REAL])
        self.assertTrue(hit)
        self.assertEqual(state["why"], "limit")
        self.assertIn("Rate limit reached", state["msg"])


if __name__ == "__main__":
    unittest.main()
