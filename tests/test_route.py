"""Deterministic tests for the delegate router. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("route", ROOT / "skills" / "delegate" / "route.py")
route = importlib.util.module_from_spec(spec)
sys.modules["route"] = route
spec.loader.exec_module(route)
import routing_support as support  # noqa: E402
from delegate.app import env
from delegate.app import usage
from delegate.app import decide as dec
from delegate.app import net
from delegate.domain.time import spec
from delegate.domain.lanes.lane import Lane
from delegate.contracts.stores import locked
from delegate.domain.health import ledger as health_ledger

net.REAL_NET_PROBE = net.net_probe
net.net_probe = lambda hosts=None: {"up": True, "ip_up": True, "why": "ok", "ms": 1, "hosts": {}, "at": time.time()}     # tests open no socket; tests/test_net.py swaps it per case
os.environ["DELEGATE_CONFIG_DIR"] = tempfile.mkdtemp()      # tests never touch a real config; classes that need their own set one
T = 1_000_000_000.0   # a fixed "now"
CFG = json.loads((ROOT / "skills" / "delegate" / "routing.json").read_text(encoding="utf-8"))


def why(r):
    """The reason of a non-done answer; None for a done one."""
    return (r["stop"] or {}).get("reason")


def at(r):
    """The time a stop says it ends or can be tried again; None when it names none."""
    return r["stop"]["retry"]["at"]


def lanes(openai=None, sub=None, credit=None):
    """Build lane states. Each arg is a dict of Lane attributes; None = healthy default."""
    mk = lambda name, fam, kind, shared=False: Lane(name, fam, kind, shared)
    o, s, c = mk("OPENAI_SUBSCRIPTION", "openai", "subscription"), mk("CLAUDE_SUBSCRIPTION", "claude", "subscription", True), mk("CLAUDE_CREDIT", "claude", "credit")
    o.five, o.week, o.pace = 10, 5, 0
    s.five, s.week, s.pace = 30, 40, 0
    c.cap, c.spend, c.reset_in_days = 150.0, 0.0, 20.0
    for lane, over in ((o, openai), (s, sub), (c, credit)):
        for k, v in (over or {}).items():
            setattr(lane, k, v)
    return {l.name: l for l in (o, s, c)}


def decide(role, ls, st=None, **args):
    a = {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False}
    a.update(args)
    return support.decide(role, copy.deepcopy(CFG), ls, st or {}, T, a)


def health_mod():
    return health_ledger


def argparse_ns(role, **kw):
    import argparse
    base = dict(role=role, authored_by=None, failed="", premium=None, allow_degraded=False, claude_usage=None, cwd=None, resume=None, cap=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _call(x):
    return x["tool_name"], x["tool_input"]


class Pick(unittest.TestCase):
    def test_worker_prefers_codex(self):
        d = decide("worker", lanes())
        self.assertEqual((d["lane"], d["model"], d["pass"]), ("OPENAI_SUBSCRIPTION", "gpt-6.1-sol", "strict"))

    def test_codex_command_handles_non_git_dirs(self):
        with tempfile.TemporaryDirectory() as d_:
            d = decide("worker", lanes(), cwd=d_)
        self.assertIn("--skip-git-repo-check", d["how"]["cli"])      # not a git work tree
        d2 = decide("worker", lanes(), cwd=str(Path(__file__).resolve().parent.parent))
        self.assertNotIn("--skip-git-repo-check", d2["how"]["cli"])  # this repo is one

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
        self.assertEqual((d["model"], d["effort"]), ("sonnet-5.5", "high"))     # Opus never runs in the loop (delegate-pnz): Sonnet is the planner's Claude rung

    def test_normal_relaxed_beats_degraded_strict(self):
        ls = lanes(openai={"out": True, "out_until": T + 1}, credit={"out": True, "out_until": T + 99}, sub={"week": 92})
        d = decide("reviewer", ls, authored_by="openai")
        self.assertEqual((d["model"], d["effort"], d["pass"]), ("sonnet-5.5", "medium", "relaxed"))


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
        d = decide("hard", ls, failed=[0])                          # Claude Sonnet is the last rung; Opus is never routed in the loop (delegate-pnz)
        self.assertEqual((d["model"], d["effort"]), ("sonnet-5.5", "high"))
        self.assertEqual(decide("hard", ls, failed=[0, 1])["action"], "stop")

    def test_reviewer_prefers_the_other_family_and_authored_by_is_optional(self):
        self.assertEqual(decide("reviewer", lanes())["model"], "gpt-6.1-sol")                 # no author given: Codex first (the most room), no claim of independence
        d = decide("reviewer", lanes(), authored_by="openai")                                  # Claude is the last lane tried (delegate-m86): Codex reviews Codex, flagged
        self.assertEqual((d["lane"], d["flag"]), ("OPENAI_SUBSCRIPTION", "same_family"))
        self.assertIn("claude is kept as the last resort", d["warning"])
        out = {"out": True, "out_until": T + 3600}
        self.assertEqual(decide("reviewer", lanes(openai=out), authored_by="openai")["lane"], "CLAUDE_SUBSCRIPTION")
        self.assertEqual(decide("reviewer", lanes(), authored_by="claude")["lane"], "OPENAI_SUBSCRIPTION")
        self.assertEqual(decide("reviewer", lanes(), authored_by="gemini")["action"], "error")

    def test_same_family_review_is_the_last_resort_and_flagged(self):
        out = {"out": True, "out_until": T + 3600}
        d = decide("reviewer", lanes(sub=out, credit=out), authored_by="openai")                  # Claude is out: Codex reviews Codex
        self.assertEqual((d["action"], d["lane"], d["flag"]), ("run", "OPENAI_SUBSCRIPTION", "same_family"))
        d = decide("reviewer", lanes(openai=out), authored_by="claude")                           # Codex is out: Claude reviews Claude
        self.assertEqual((d["action"], d["lane"], d["flag"]), ("run", "CLAUDE_SUBSCRIPTION", "same_family"))
        d = decide("reviewer", lanes(openai=out, sub=out, credit=out), authored_by="claude")
        self.assertEqual(d["action"], "stop")                                                # nobody left: stop, not an exception

    def test_every_reviewer_list_reaches_every_family(self):
        for who, steps in CFG["roles"]["reviewer"]["by_author"].items():
            self.assertEqual({s["on"] for s in steps}, {"openai", "claude", "deepseek", "zai"}, who)      # Grok never reviews (delegate-qjw)
            if who not in ("unknown", "xai"):
                fam = who
                self.assertEqual(steps[-1]["flag"], "same_family", who)
                self.assertEqual(steps[-1]["on"], fam, who)

    def test_only_one_family_up_never_stops_and_warns(self):
        out = {"out": True, "out_until": T + 3600}
        grok = Lane("GROK_SUBSCRIPTION", "xai", "subscription")
        cases = {"openai": lambda: lanes(sub=out, credit=out), "claude": lambda: lanes(openai=out),
                 }
        for who, mk in cases.items():
            ls = mk()
            if who != "xai":
                grok.out, grok.out_until, grok.out_reason = True, T + 7200, "limit"
            ls["GROK_SUBSCRIPTION"] = grok if who != "xai" else Lane("GROK_SUBSCRIPTION", "xai", "subscription")
            if who != "xai":
                ls["GROK_SUBSCRIPTION"] = grok
            d = decide("reviewer", ls, authored_by=who)
            self.assertEqual((d["action"], d["flag"]), ("run", "same_family"), who)
            self.assertEqual(d["lane"].split("_")[0], {"openai": "OPENAI", "claude": "CLAUDE"}[who], who)
            self.assertIn(f"reviewing {who}'s work with {who}: no other family is up", d["warning"], who)
            ud = decide("reviewer", ls, authored_by=who, unattended=True, allow_degraded=True)      # no consent step exists for review
            self.assertEqual((ud["action"], ud["flag"]), ("run", "same_family"), who)

    def test_warning_names_why_other_families_are_down(self):
        out = {"out": True, "out_until": T + 3600, "out_reason": "logged_out"}
        ls = lanes(sub=out, credit=out)
        g = Lane("GROK_SUBSCRIPTION", "xai", "subscription")
        g.out, g.out_until = True, T + 7200
        ls["GROK_SUBSCRIPTION"] = g
        w = decide("reviewer", ls, authored_by="openai")["warning"]
        self.assertIn("xai out until", w)
        self.assertIn("claude logged out", w)
        self.assertNotIn("warning", decide("reviewer", lanes(), authored_by="claude"))

    def test_premium_only_when_requested(self):
        self.assertNotEqual(decide("hard", lanes())["model"], "fable-5.1")
        d = decide("worker", lanes(), premium="claude")
        self.assertEqual((d["model"], d["flag"]), ("fable-5.1", "user_requested_premium"))
        d = decide("worker", lanes(), premium="openai")
        self.assertEqual(d["action"], "error")      # no premium model mapped for openai yet

    def test_light_uses_luna_first(self):
        d = decide("light", lanes())
        self.assertEqual((d["model"], d["mode"]), ("gpt-6-luna", "read_only"))


class Helpers(unittest.TestCase):
    def test_unknown_usage_shapes_are_rejected(self):
        self.assertIsNone(usage.normalize_usage({"x": 1}))
        self.assertIsNone(usage.normalize_usage({"five_hour": {"used": 37}}))      # only get_usage's real shape is understood

    def test_real_get_usage_result_is_understood(self):
        real = {"plan": {"status": "ok", "plan": "Max", "windows": [
            {"label": "5-hour limit", "percentUsed": 1, "resetsAt": "2026-10-18T21:29:59.908Z", "resetsIn": "4h 47m"},
            {"label": "Weekly \u00b7 all models", "percentUsed": 3, "resetsAt": "2026-10-25T13:59:59.908Z", "resetsIn": "6d 21h"},
            {"label": "Weekly \u00b7 Fable", "percentUsed": 1, "resetsAt": "2026-10-25T13:59:59.908Z"}],
            "extraUsage": {"enabled": True}}, "context": {"percentUsed": 25}}
        u = usage.normalize_usage(real)
        self.assertEqual((u["five_hour"]["used"], u["weekly"]["used"], u["fable"]["used"]), (1.0, 3.0, 1.0))
        self.assertAlmostEqual(u["weekly"]["resets_at"], dt.datetime(2026, 10, 25, 13, 59, 59, 908000, tzinfo=dt.timezone.utc).timestamp(), places=2)
        self.assertEqual(usage.normalize_usage(real["plan"]), u)

    def test_next_weekly(self):
        t = dt.datetime(2026, 10, 18, 17, 1).timestamp()          # Sunday just after 17:00
        n = dt.datetime.fromtimestamp(spec.next_weekly("Sun 17:00", t))
        self.assertEqual((n.weekday(), n.hour, n.day), (6, 17, 25))


class Cli(unittest.TestCase):
    def test_report_and_next_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = os.environ["DELEGATE_CONFIG_DIR"] = d
            os.environ["APPDATA"] = d
            os.environ["DELEGATE_NOW"] = str(T)
            try:
                route.main(["report", "--lane", "OPENAI_SUBSCRIPTION", "--event", "out", "--until", str(T + 3600)])
                st = env.load_state()
                self.assertIn("OPENAI_SUBSCRIPTION", st.out)
                cfg = env.load_routing()
                real = dict(usage.READERS)
                usage.READERS.update({k: (lambda lane, S, st_, t, *a: None) for k in real})
                try:
                    ls = dec.fleet_builder(cfg).build(st, T, check_config=False).lanes
                finally:
                    usage.READERS.update(real)
                self.assertTrue(ls["OPENAI_SUBSCRIPTION"].out)
                route.main(["report", "--lane", "OPENAI_SUBSCRIPTION", "--event", "ok"])
                self.assertNotIn("OPENAI_SUBSCRIPTION", env.load_state().out)
            finally:
                for k in ("XDG_CONFIG_HOME", "APPDATA", "DELEGATE_NOW", "DELEGATE_CONFIG_DIR"):
                    os.environ.pop(k, None)


class BaseTable(unittest.TestCase):
    """SKILL.md carries a fixed role table for installs without Python. It must equal what routing.json gives for Claude sub-agents."""

    def test_skill_table_matches_routing_json(self):
        import re
        skill = (Path(__file__).resolve().parent.parent / "skills" / "delegate" / "SKILL.md").read_text(encoding="utf-8")
        block = re.search(r"<!-- base-table -->(.*?)<!-- /base-table -->", skill, re.S).group(1)
        table = {}
        for line in block.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] not in ("role", "---", ""):
                table[cells[0]] = cells[1]
        cfg = env.load_routing()
        want = {}
        for name, role in cfg["roles"].items():
            steps = role["by_author"]["openai"] if "by_author" in role else role["steps"]
            seq = [f"{cfg['models'][s['model']]['agent']} {s['effort']}" for s in steps if s["on"] == "claude" and s.get("flag") != "degraded"]
            want[name] = " > ".join(seq)
        self.assertEqual(table, want)


class StateLock(unittest.TestCase):
    def test_a_lock_that_stays_held_raises_instead_of_running_unlocked(self):
        with tempfile.TemporaryDirectory() as d:
            with locked(Path(d) / "x", timeout=5):
                with self.assertRaises(TimeoutError):
                    with locked(Path(d) / "x", timeout=0.2):
                        pass

    def test_parallel_processes_lose_no_update(self):
        code = ("import sys; sys.path.insert(0, %r); from delegate.app import env\n"
                "for _ in range(25): env.update_state(lambda s: s.warned_at.__setitem__('n', s.warned_at.get('n', 0) + 1))" % str(ROOT / "src"))
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, DELEGATE_CONFIG_DIR=d)
            ps = [subprocess.Popen([sys.executable, "-c", code], env=env, stderr=subprocess.PIPE) for _ in range(8)]
            self.maxDiff = None
            self.assertEqual([p.communicate()[1].decode()[-1500:] for p in ps], [""] * 8)
            self.assertEqual(json.loads(Path(d, "state.json").read_text())["warned_at"]["n"], 200)


if __name__ == "__main__":
    unittest.main()
