"""Idle watchdog (R15: delegate-go0). The health fold, breaker and incident replays live in tests/domain/test_health.py."""
import hermetic  # noqa: F401  (a home of its own for every test)
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "delegate"))
from delegate.app import watch      # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run_watch(cli, events_at, until, tick=0.5, jump=None):
    """Drive a Watchdog on a fake clock like run_proc does (a poll every `tick` s). -> the second it fired, or None."""
    clk = Clock()
    wd = watch.Watchdog(clock=clk, **watch.limits(cli))
    events = sorted(events_at)
    while clk.t < until:
        clk.t = round(clk.t + tick, 3)
        while events and events[0] <= clk.t:
            events.pop(0)
            wd.touch()
        if jump and jump[0] <= clk.t < jump[0] + tick:
            clk.t += jump[1]
        if wd.poll():
            return clk.t, wd
    return None, wd


class Watchdog(unittest.TestCase):
    def test_answer_grace_is_30s_and_the_codex_silence_limit_is_unchanged(self):
        """delegate-itn: the grace after a final answer is 120 s, a stalled model request 900 s; a stream with no final message still waits the 3600 s silence limit."""
        self.assertEqual(watch.ANSWER_GRACE_S, 120)
        self.assertEqual(watch.STALL_S, 900)
        self.assertEqual(watch.LIMITS["codex"]["silence_s"], 3600)
        at, _ = run_watch("codex", [5], 6000)
        self.assertEqual(at, 3605.5)

    def test_limits(self):
        L = watch.LIMITS
        self.assertEqual({c: (L[c]["first_event_s"], L[c]["silence_s"]) for c in L}, {"claude": (120, 600), "codex": (120, 3600), "opencode": (120, 900)})
        self.assertGreaterEqual(L["codex"]["silence_s"], 3600)         # P7: healthy Codex reasoning gaps up to 2,298 s
        self.assertNotEqual(L["codex"]["silence_s"], 600)
        self.assertGreaterEqual(L["opencode"]["silence_s"], 900)

    def test_injected_1800s_codex_reasoning_gap_is_not_killed(self):
        at, _ = run_watch("codex", [10, 12, 10 + 1800 + 2, 10 + 1800 + 4], 10 + 1800 + 100)
        self.assertIsNone(at)

    def test_injected_codex_gap_of_2298s_is_not_killed_but_a_dead_stream_is(self):
        self.assertIsNone(run_watch("codex", [5, 5 + 2298], 5 + 2298 + 60)[0])
        at, wd = run_watch("codex", [5], 6000)
        self.assertEqual(wd.why, "silence")
        self.assertAlmostEqual(at, 5 + 3600, delta=1.0)

    def test_codex_3000s_gap_survives_and_is_logged_but_1800s_is_not(self):
        for gap, logged in ((1800, []), (3000, [3000.0])):
            at, wd = run_watch("codex", [5, 5 + gap], 5 + gap + 60)
            self.assertIsNone(at)
            rows = []
            wd.flush(rows.append, "OPENAI_SUBSCRIPTION", "gpt", True)
            self.assertEqual([r["gap_s"] for r in rows], logged)
            for r in rows:
                self.assertEqual((r["lane"], r["model"], r["finished"], r["run_id"]), ("OPENAI_SUBSCRIPTION", "gpt", True, None))

    def test_codex_3700s_dead_stream_is_killed_and_logged(self):
        at, wd = run_watch("codex", [5], 6000)       # run_proc turns this verdict into route.stop_tree (covered in test_route)
        self.assertEqual(wd.why, "silence")
        self.assertLess(at, 3700)
        rows = []
        wd.flush(rows.append, "L", "m", False)
        self.assertEqual([(r["finished"], r["gap_s"] > 1800) for r in rows], [(False, True)])
        wd.flush(lambda r: 1 / 0, "L", "m", False)      # a failing writer never fails the call

    def test_claude_silence_600_and_opencode_900(self):
        at, wd = run_watch("claude", [3], 2000)
        self.assertEqual((wd.why, round(at)), ("silence", 604))
        at, wd = run_watch("opencode", [3], 2000)
        self.assertEqual((wd.why, round(at)), ("silence", 904))

    def test_no_first_event_within_120s_is_hung(self):
        at, wd = run_watch("codex", [], 1000)
        self.assertEqual(wd.why, "first_event")
        self.assertAlmostEqual(at, 120, delta=1.0)
        self.assertIn("printed nothing within 120s", wd.describe())

    def test_any_line_is_activity(self):
        self.assertIsNone(run_watch("claude", list(range(5, 3000, 590)), 3000)[0])

    def test_sleep_gap_is_not_silence(self):
        # one poll sees the clock jump 2,000 s (the lid was shut): that time is not counted against a Claude worker with a 600 s limit
        at, wd = run_watch("claude", [5], 400, jump=(100, 2000))
        self.assertIsNone(at)

    def test_p95_hung_step_time_is_idle_limit_plus_at_most_one_tick(self):
        import random
        rnd, lat = random.Random(7), []
        for i in range(200):
            cli = ("claude", "codex", "opencode")[i % 3]
            lim = watch.LIMITS[cli]
            last_event = rnd.uniform(1, 300)                 # the worker dies after a random amount of healthy output
            at, wd = run_watch(cli, [last_event / 2, last_event], last_event + lim["silence_s"] + 300)
            self.assertIsNotNone(at, cli)
            lat.append(at - last_event - lim["silence_s"])
        lat.sort()
        self.assertLessEqual(lat[int(len(lat) * 0.95)], 180)             # the bar: idle limit + 3 min
        self.assertLessEqual(lat[-1], 1.0)                                # in practice: one poll tick

    def test_heartbeat_is_throttled(self):
        clk, beats = Clock(), []
        wd = watch.Watchdog(120, 600, clock=clk, beat=beats.append, wall=lambda: 1e9 + clk.t)
        for i in range(100):
            clk.t = i
            wd.touch()
        self.assertEqual(len(beats), 4)       # at 0, 30, 60, 90


if __name__ == "__main__":
    unittest.main()
