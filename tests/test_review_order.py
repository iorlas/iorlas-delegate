"""Reviewer order (delegate-jx2): another non-Claude family first, then the writer's own non-Claude family (same_family warning, never blocks),
then Claude last. Claude-last holds for every other role too."""
import contextlib
import copy
import importlib.util
import io
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

os.environ["DELEGATE_CONFIG_DIR"] = tempfile.mkdtemp()
T = 1_000_000_000.0
CFG = json.loads((ROOT / "skills" / "delegate" / "routing.json").read_text(encoding="utf-8"))
CFG["settings"]["deepseek_free_enabled"] = True
CFG["settings"]["zai_coding_enabled"] = True

NAMES = {"openai": ("OPENAI_SUBSCRIPTION", "subscription"), "xai": ("GROK_SUBSCRIPTION", "subscription"), "deepseek": ("DEEPSEEK_FREE", "free"),
         "zai": ("ZAI_CODING_SUBSCRIPTION", "subscription"), "claude": ("CLAUDE_SUBSCRIPTION", "subscription")}


def lanes(*up):
    """Only the families in `up` are healthy; every other family is out."""
    out = {}
    for fam, (name, kind) in NAMES.items():
        l = route.Lane(name, fam, CFG["lanes"][name]["kind"], fam == "claude")
        l.five, l.week, l.pace = 10, 5, 0
        if fam not in up:
            l.out, l.out_until, l.out_reason = True, T + 3600, "limit"
        out[name] = l
    return out


def review(writer, *up, role="reviewer", **kw):
    a = {"authored_by": writer, "failed": [], "premium": None, "allow_degraded": False}
    a.update(kw)
    return route.decide(role, copy.deepcopy(CFG), lanes(*up), {}, T, a)


ALL = tuple(NAMES)


class ReviewOrder(unittest.TestCase):
    def fam(self, d):
        return CFG["lanes"][d["lane"]]["family"]

    def test_codex_writer_gets_another_non_claude_family_first(self):
        for others in (("xai",), ("zai",), ("deepseek",), ("xai", "zai", "deepseek")):
            d = review("openai", "openai", "claude", *others)
            self.assertIn(self.fam(d), others)
            self.assertNotIn("warning", d)
            self.assertNotEqual(d.get("flag"), "same_family")

    def test_codex_writer_with_only_codex_non_claude_reviews_itself_before_claude(self):
        d = review("openai", "openai", "claude")
        self.assertEqual((d["lane"], d["flag"]), ("OPENAI_SUBSCRIPTION", "same_family"))
        self.assertTrue(d["warning"].startswith("reviewing openai's work with openai"))

    def test_codex_writer_with_no_non_claude_up_goes_to_claude(self):
        d = review("openai", "claude")
        self.assertEqual((d["action"], self.fam(d)), ("run", "claude"))
        self.assertEqual(d["flag"], None)           # independent Claude is not a same-family review

    def test_claude_writer_prefers_any_non_claude_then_claude_with_warning(self):
        self.assertEqual(self.fam(review("claude", *ALL)), "openai")
        for fam in ("xai", "zai", "deepseek"):
            d = review("claude", "claude", fam)
            self.assertEqual(self.fam(d), fam)
            self.assertNotIn("warning", d)
        d = review("claude", "claude")
        self.assertEqual((self.fam(d), d["flag"]), ("claude", "same_family"))
        self.assertIn("reviewing claude's work with claude", d["warning"])

    def test_zai_writer_other_families_then_zai_then_claude(self):
        d = review("zai", *ALL)
        self.assertIn(self.fam(d), ("deepseek", "openai", "xai"))
        d = review("zai", "zai", "claude")
        self.assertEqual((d["lane"], d["flag"]), ("ZAI_CODING_SUBSCRIPTION", "same_family"))
        d = review("zai", "claude")
        self.assertEqual((self.fam(d), d["flag"]), ("claude", None))

    def test_deepseek_and_xai_writers_follow_the_same_rule(self):
        for who, lane in (("deepseek", "DEEPSEEK_FREE"), ("xai", "GROK_SUBSCRIPTION")):
            d = review(who, who, "claude")
            self.assertEqual((d["lane"], d["flag"]), (lane, "same_family"), who)
            self.assertEqual(self.fam(review(who, "claude")), "claude", who)

    def test_only_one_family_up_never_blocks_even_unattended(self):
        for who in ("openai", "claude", "xai", "zai", "deepseek"):
            d = review(who, who, unattended=True, allow_degraded=True)
            self.assertEqual((d["action"], d["flag"]), ("run", "same_family"), who)
            self.assertIn(f"reviewing {who}'s work with {who}", d["warning"], who)
        self.assertEqual(review("openai")["action"], "stop")

    def test_claude_stays_last_for_other_roles(self):
        for role in ("worker", "planner"):
            d = review(None, "openai", "claude", role=role)
            self.assertEqual(self.fam(d), "openai", role)
        d = review(None, "claude", "xai", role="worker")
        self.assertEqual(self.fam(d), "xai")

    def test_next_prints_the_warning_on_stderr_and_in_the_result(self):
        d = review("openai", "openai", "claude")
        route_decision = route.decision
        route.decision = lambda a, cfg: (copy.deepcopy(d), lanes("openai", "claude"), [], T)
        err, out = io.StringIO(), io.StringIO()
        try:
            ns = type("A", (), {"verbose": False})()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                self.assertEqual(route.cmd_next(ns, CFG), 0)
        finally:
            route.decision = route_decision
        self.assertIn("warning: reviewing openai's work with openai", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["warning"], d["warning"])


if __name__ == "__main__":
    unittest.main()
