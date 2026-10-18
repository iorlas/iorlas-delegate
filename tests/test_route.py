"""Deterministic tests for the delegate router. Run: python -m unittest discover -s tests"""
import copy
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("route", ROOT / "skills" / "delegate" / "route.py")
route = importlib.util.module_from_spec(spec)
sys.modules["route"] = route
spec.loader.exec_module(route)

T = 1_000_000_000.0   # a fixed "now"
CFG = json.load(open(ROOT / "skills" / "delegate" / "routing.json"))


def lanes(openai=None, sub=None, credit=None):
    """Build lane states. Each arg is a dict of Lane attributes; None = healthy default."""
    mk = lambda name, fam, kind, shared=False: route.Lane(name, fam, kind, shared)
    o, s, c = mk("OPENAI_SUBSCRIPTION", "openai", "subscription"), mk("CLAUDE_SUBSCRIPTION", "claude", "subscription", True), mk("CLAUDE_CREDIT", "claude", "credit")
    o.five, o.week, o.pace = 10, 5, 0
    s.five, s.week, s.pace = 30, 40, 0
    c.cap, c.spend, c.reset_in_days = 150.0, 0.0, 20.0
    for lane, over in ((o, openai), (s, sub), (c, credit)):
        for k, v in (over or {}).items():
            setattr(lane, k, v)
    return {l.name: l for l in (o, s, c)}


def decide(role, ls, st=None, **args):
    a = {"writer": None, "failed": [], "fable": False, "allow_degraded": False}
    a.update(args)
    return route.decide(role, copy.deepcopy(CFG), ls, st or {}, T, a)


class Pick(unittest.TestCase):
    def test_worker_prefers_codex(self):
        d = decide("worker", lanes())
        self.assertEqual((d["lane"], d["model"], d["pass"]), ("OPENAI_SUBSCRIPTION", "gpt-6.1-sol", "strict"))

    def test_codex_out_falls_to_claude_subscription_when_healthy(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 3600}))
        self.assertEqual((d["lane"], d["model"]), ("CLAUDE_SUBSCRIPTION", "sonnet-5.5"))

    def test_strained_subscription_uses_credit(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}, sub={"five": 85}))
        self.assertEqual(d["lane"], "CLAUDE_CREDIT")

    def test_credit_out_goes_to_subscription(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 999}))
        self.assertEqual(d["lane"], "CLAUDE_SUBSCRIPTION")

    def test_burn_prefers_subscription(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}, sub={"burn": True, "five": 30}))
        self.assertEqual(d["lane"], "CLAUDE_SUBSCRIPTION")

    def test_expiring_credit_is_spent(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}, credit={"reset_in_days": 2.0, "spend": 10.0}))
        self.assertEqual(d["lane"], "CLAUDE_CREDIT")

    def test_unknown_subscription_usage_prefers_credit(self):
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}, sub={"five": None, "week": None, "pace": None}))
        self.assertEqual(d["lane"], "CLAUDE_CREDIT")

    def test_dwell_keeps_previous_choice(self):
        st = {"prev": {"claude": {"lane": "CLAUDE_CREDIT", "since": T - 600}}}
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}), st)
        self.assertEqual(d["lane"], "CLAUDE_CREDIT")
        st = {"prev": {"claude": {"lane": "CLAUDE_CREDIT", "since": T - 3600}}}
        d = decide("worker", lanes(openai={"out": True, "out_until": T + 1}), st)
        self.assertEqual(d["lane"], "CLAUDE_SUBSCRIPTION")


class SecondPass(unittest.TestCase):
    def test_relaxed_pass_uses_reserved_subscription_same_step(self):
        ls = lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 99}, sub={"week": 92, "five": 40})
        d = decide("worker", ls)
        self.assertEqual((d["lane"], d["model"], d["effort"], d["pass"]), ("CLAUDE_SUBSCRIPTION", "sonnet-5.5", "medium", "relaxed"))

    def test_relaxed_has_a_ceiling(self):
        ls = lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 99}, sub={"week": 96})
        d = decide("worker", ls)
        self.assertEqual(d["action"], "stop")

    def test_relaxed_never_changes_model_floor(self):
        ls = lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 99}, sub={"week": 92})
        d = decide("planner", ls)
        self.assertEqual((d["model"], d["effort"]), ("opus-5.5", "medium"))

    def test_normal_relaxed_beats_degraded_strict(self):
        ls = lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 99}, sub={"week": 92})
        d = decide("reviewer", ls, writer="openai")
        self.assertEqual((d["model"], d["effort"], d["pass"]), ("opus-5.5", "medium", "relaxed"))


class Roles(unittest.TestCase):
    def test_planner_stop_asks_consent(self):
        ls = lanes(openai={"out": True, "out_until": T + 5000}, credit={"out": True, "out_until": T + 9000}, sub={"out": True, "out_until": T + 7000})
        d = decide("planner", ls)
        self.assertEqual(d["action"], "stop")
        self.assertIn("ask_user", d)
        self.assertEqual(len(d["resets"]), 3)

    def test_planner_degraded_only_with_consent(self):
        ls = lanes(openai={"out": True, "out_until": T + 5000}, credit={"out": True, "out_until": T + 9000}, sub={"week": 96, "five": 10})
        self.assertEqual(decide("planner", ls)["action"], "stop")
        ls2 = lanes(openai={"out": True, "out_until": T + 5000}, credit={"out": True, "out_until": T + 9000}, sub={"week": 96, "five": 10})
        self.assertEqual(decide("planner", ls2, allow_degraded=True)["action"], "stop")   # ceiling still binds

    def test_hard_escalates_on_failure_only(self):
        ls = lanes()
        self.assertEqual(decide("hard", ls)["model"], "gpt-6.1-sol")
        d = decide("hard", ls, failed=[0])
        self.assertEqual((d["model"], d["effort"]), ("opus-5.5", "low"))
        d = decide("hard", ls, failed=[0, 1])
        self.assertEqual((d["model"], d["effort"]), ("opus-5.5", "medium"))
        self.assertEqual(decide("hard", ls, failed=[0, 1, 2])["action"], "stop")

    def test_reviewer_needs_writer_and_crosses_family(self):
        self.assertEqual(decide("reviewer", lanes())["action"], "error")
        d = decide("reviewer", lanes(), writer="openai")
        self.assertEqual(d["lane"] in ("CLAUDE_SUBSCRIPTION", "CLAUDE_CREDIT"), True)
        d = decide("reviewer", lanes(), writer="claude")
        self.assertEqual((d["lane"], d["use"]), ("OPENAI_SUBSCRIPTION", "review"))

    def test_fable_only_when_requested(self):
        self.assertNotEqual(decide("hard", lanes())["model"], "fable-5.1")
        d = decide("worker", lanes(), fable=True)
        self.assertEqual(d["model"], "fable-5.1")

    def test_light_uses_luna_first(self):
        d = decide("light", lanes())
        self.assertEqual((d["model"], d["mode"]), ("gpt-6-luna", "read_only"))


class Helpers(unittest.TestCase):
    def test_normalize_usage_shapes(self):
        n = route.normalize_usage({"five_hour": {"used_percent": 37, "resets_at": T + 100}, "weekly": {"utilization": 52, "resetsAt": T + 1000}}, T)
        self.assertEqual((n["five_hour"]["used"], n["weekly"]["used"]), (37.0, 52.0))
        self.assertIsNone(route.normalize_usage({"x": 1}, T))

    def test_window_after_reset_is_zero(self):
        self.assertEqual(route.window(95, T - 5, 300, T)[0], 0.0)

    def test_credit_cycle(self):
        t = dt.datetime(2026, 10, 18, 12).timestamp()
        s, n = route.credit_cycle(1, t)
        self.assertEqual((dt.datetime.fromtimestamp(s).date(), dt.datetime.fromtimestamp(n).date()), (dt.date(2026, 10, 1), dt.date(2026, 11, 1)))

    def test_next_weekly(self):
        t = dt.datetime(2026, 10, 18, 17, 1).timestamp()          # Sunday just after 17:00
        n = dt.datetime.fromtimestamp(route.next_weekly("Sun 17:00", t))
        self.assertEqual((n.weekday(), n.hour, n.day), (6, 17, 25))


class Cli(unittest.TestCase):
    def test_report_and_next_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            os.environ["APPDATA"] = d
            os.environ["DELEGATE_NOW"] = str(T)
            try:
                route.main(["report", "--lane", "OPENAI_SUBSCRIPTION", "--event", "out", "--until", str(T + 3600), "--message", "test"])
                st = route.load_state()
                self.assertIn("OPENAI_SUBSCRIPTION", st["out"])
                cfg = route.load_routing()
                real = dict(route.READERS)
                route.READERS.update({k: (lambda lane, S, st_, t, *a: None) for k in real})
                try:
                    ls = route.gather(cfg, st, T)
                finally:
                    route.READERS.update(real)
                self.assertTrue(ls["OPENAI_SUBSCRIPTION"].out)
                route.main(["report", "--lane", "OPENAI_SUBSCRIPTION", "--event", "ok"])
                self.assertNotIn("OPENAI_SUBSCRIPTION", route.load_state().get("out", {}))
            finally:
                for k in ("XDG_CONFIG_HOME", "APPDATA", "DELEGATE_NOW"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
