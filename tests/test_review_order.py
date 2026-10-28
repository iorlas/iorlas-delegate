"""Reviewer order (delegate-jx2, qjw, pnz): Codex first, then the other non-Claude families cheapest configured cost first (Grok never reviews), then the
writer's own non-Claude family (same_family warning, never blocks), then Claude Sonnet last; Opus never runs in the loop. Claude-last holds for every other role too."""
import hermetic  # noqa: F401  (a home of its own for every test)
import routing_support as support
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
from delegate.app import decide as dec
from delegate.domain.time import spec
from delegate.domain.lanes.lane import Lane
from delegate.cli import verbs

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
        l = Lane(name, fam, CFG["lanes"][name]["kind"], fam == "claude")
        l.five, l.week, l.pace = 10, 5, 0
        if fam not in up:
            l.out, l.out_until, l.out_reason = True, T + 3600, "limit"
        out[name] = l
    return out


def review(writer, *up, role="reviewer", **kw):
    a = {"authored_by": writer, "failed": [], "premium": None, "allow_degraded": False}
    a.update(kw)
    return support.decide(role, copy.deepcopy(CFG), lanes(*up), {}, T, a)


ALL = tuple(NAMES)


class ReviewOrder(unittest.TestCase):
    def fam(self, d):
        return CFG["lanes"][d["lane"]]["family"]

    def test_codex_writer_gets_another_non_claude_family_first(self):
        for others in (("zai",), ("deepseek",), ("zai", "deepseek")):
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
        for fam in ("zai", "deepseek"):
            d = review("claude", "claude", fam)
            self.assertEqual(self.fam(d), fam)
            self.assertNotIn("warning", d)
        d = review("claude", "claude")
        self.assertEqual((self.fam(d), d["flag"]), ("claude", "same_family"))
        self.assertIn("reviewing claude's work with claude", d["warning"])

    def test_zai_writer_other_families_then_zai_then_claude(self):
        d = review("zai", *ALL)
        self.assertIn(self.fam(d), ("deepseek", "openai"))
        d = review("zai", "zai", "claude")
        self.assertEqual((d["lane"], d["flag"]), ("ZAI_CODING_SUBSCRIPTION", "same_family"))
        d = review("zai", "claude")
        self.assertEqual((self.fam(d), d["flag"]), ("claude", None))

    def test_deepseek_and_xai_writers_follow_the_same_rule(self):
        d = review("deepseek", "deepseek", "claude")
        self.assertEqual((d["lane"], d["flag"]), ("DEEPSEEK_FREE", "same_family"))
        self.assertEqual(self.fam(review("deepseek", "claude")), "claude")
        d = review("xai", "xai", "claude")                  # a Grok writer is never reviewed by Grok: next rung
        self.assertEqual((self.fam(d), d["model"]), ("claude", "sonnet-5.5"))

    def test_only_one_family_up_never_blocks_even_unattended(self):
        for who in ("openai", "claude", "zai", "deepseek"):
            d = review(who, who, unattended=True, allow_degraded=True)
            self.assertEqual((d["action"], d["flag"]), ("run", "same_family"), who)
            self.assertIn(f"reviewing {who}'s work with {who}", d["warning"], who)
        self.assertEqual(review("openai")["action"], "stop")
        self.assertEqual(review("xai", "xai")["action"], "stop")        # only Grok up: no review lane at all

    def test_claude_stays_last_for_other_roles(self):
        for role in ("worker", "planner"):
            d = review(None, "openai", "claude", role=role)
            self.assertEqual(self.fam(d), "openai", role)
        d = review(None, "claude", "xai", role="worker")
        self.assertEqual(self.fam(d), "xai")

    def order(self, writer, *up):
        """Lanes the reviewer would use, in order, as each is taken down in turn."""
        up, out = list(up), []
        while True:
            d = review(writer, *up)
            if d["action"] != "run":
                return out
            out.append((self.fam(d), d["model"], d["flag"]))
            up.remove(self.fam(d)) if (self.fam(d) != "claude" or d["flag"] != "degraded") else up.remove("claude")

    def test_grok_never_reviews_even_when_it_is_the_only_lane_up(self):
        for writer in (None, "openai", "claude", "zai", "deepseek", "xai"):
            self.assertEqual(review(writer, "xai")["action"], "stop", writer)
            d = review(writer, "xai", "claude")
            self.assertEqual((self.fam(d), d["model"]), ("claude", "sonnet-5.5"), writer)
            for fams in (ALL, ("xai", "openai"), ("xai", "zai"), ("xai", "deepseek", "claude")):
                self.assertNotEqual(self.fam(review(writer, *fams)), "xai", (writer, fams))

    def test_grok_still_routes_for_the_worker_role(self):
        self.assertEqual(review(None, "xai", role="worker")["lane"], "GROK_SUBSCRIPTION")
        self.assertEqual(review(None, "xai", "claude", role="worker")["model"], "grok-4.7")

    def test_order_writer_codex(self):
        self.assertEqual([x[0] for x in self.order("openai", *ALL)], ["zai", "deepseek", "openai", "claude"])
        d = review("openai", "openai", "xai", "claude")
        self.assertEqual((self.fam(d), d["flag"]), ("openai", "same_family"))

    def test_order_writer_claude(self):
        d = review("claude", *ALL)
        self.assertEqual(d["lane"], "OPENAI_SUBSCRIPTION")
        self.assertEqual([x[:2] for x in self.order("claude", *ALL)][:3], [("openai", "gpt-6.1-sol"), ("zai", "glm-5.3"), ("deepseek", "deepseek-v4.1-flash-free")])
        self.assertEqual(self.order("claude", "claude", "xai")[0][:2], ("claude", "sonnet-5.5"))

    def test_order_writer_zai_deepseek_xai(self):
        self.assertEqual(self.order("zai", *ALL)[0][0], "openai")           # Codex first wherever it is another family
        self.assertEqual(self.order("zai", "zai", "deepseek", "claude")[0][0], "deepseek")
        self.assertEqual([x[0] for x in self.order("deepseek", *ALL)], ["openai", "zai", "deepseek", "claude"])
        self.assertEqual([x[0] for x in self.order("xai", *ALL)], ["openai", "zai", "deepseek", "claude"])

    def test_cheapest_first_follows_configured_cost_not_name(self):
        def first(cost):
            cfg = copy.deepcopy(CFG)
            for m, v in cost.items():
                cfg["models"][m]["cost"] = v
            a = {"authored_by": "claude", "failed": [], "premium": None, "allow_degraded": False}
            return support.decide("reviewer", cfg, lanes("zai", "deepseek", "claude"), {}, T, a)["model"]
        self.assertEqual(first({"glm-5.3": 1, "deepseek-v4.1-flash-free": 2}), "glm-5.3")
        self.assertEqual(first({"glm-5.3": 5, "deepseek-v4.1-flash-free": 2}), "deepseek-v4.1-flash-free")
        cfg = copy.deepcopy(CFG)
        cfg["models"]["gpt-6.1-sol"]["cost"] = 99                           # Codex only goes first because it is configured cheapest
        a = {"authored_by": "claude", "failed": [], "premium": None, "allow_degraded": False}
        self.assertEqual(support.decide("reviewer", cfg, lanes(*ALL), {}, T, a)["model"], "glm-5.3")

    def test_opus_is_never_returned_for_any_loop_role(self):
        import itertools
        fams = ("openai", "xai", "deepseek", "zai", "claude")
        for role in CFG["roles"]:
            for n in range(len(fams) + 1):
                for up in itertools.combinations(fams, n):
                    for writer in (None, "openai", "claude", "xai", "zai", "deepseek"):
                        for kw in ({}, {"allow_degraded": True}, {"failed": [0]}, {"failed": [0, 1]}, {"premium": "claude"}):
                            d = review(writer, *up, role=role, **kw)
                            self.assertNotEqual(d.get("model"), "opus-5.5", (role, up, writer, kw))

    def test_only_claude_up_gets_sonnet_never_opus(self):
        d = review("openai", "claude")
        self.assertEqual((d["action"], d["model"]), ("run", "sonnet-5.5"))
        for role in ("planner", "hard"):                                    # their Claude rung is Sonnet, never Opus
            d = review("openai", "claude", role=role)
            self.assertEqual((d["action"], d["model"]), ("run", "sonnet-5.5"), role)

    def test_opus_only_config_returns_no_lane_with_a_reason(self):
        cfg = copy.deepcopy(CFG)
        for name in ("reviewer", "planner", "hard"):
            role = cfg["roles"][name]
            opus = dict(model="opus-5.5", effort="medium", on="claude", mode="read_only", cap=3)
            role.pop("degraded_needs_consent", None)
            if "by_author" in role:
                role["by_author"] = {k: [dict(opus)] for k in role["by_author"]}
            else:
                role["steps"] = [dict(opus)]
            d = support.decide(name, cfg, lanes(*ALL), {}, T, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": True})
            self.assertEqual((d["action"], d.get("reason")), ("stop", "loop_excluded"), name)
            self.assertIn("opus-5.5 is never routed in the loop", d["say"])

    def test_opus_is_a_model_flag_so_an_explicit_call_can_still_name_it(self):
        self.assertIs(CFG["models"]["opus-5.5"]["loop"], False)         # the router's chains skip it; a caller naming Opus directly (outside the loop) is not routed

    def test_next_prints_the_warning_on_stderr_and_in_the_result(self):
        d = review("openai", "openai", "claude")
        route_decision = dec.decision
        dec.decision = lambda a, cfg: (copy.deepcopy(d), lanes("openai", "claude"), [], T)
        err, out = io.StringIO(), io.StringIO()
        try:
            ns = type("A", (), {"verbose": False})()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                self.assertEqual(verbs.cmd_next(ns, CFG), 0)
        finally:
            dec.decision = route_decision
        self.assertIn("warning: reviewing openai's work with openai", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["warning"], d["warning"])


if __name__ == "__main__":
    unittest.main()
