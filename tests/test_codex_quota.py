"""Codex quota read (delegate-av1) and Claude-last routing (delegate-m86), with responses recorded from `codex app-server` on 2026-10-26."""
import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import os
import tempfile
import unittest
import routing_support as support
from delegate.contracts.state import UsageCache
from test_route import route, CFG, lanes, decide, T
import onboard

# `account/rateLimits/read` as recorded: Pro Lite, one weekly window sent as `primary`, no `secondary`, no other bucket, no credits.
RECORDED = {"ordinaryUsageAllowed": True, "rateLimits": {
    "limitId": "codex", "limitName": None, "normalModelSlug": None,
    "primary": {"usedPercent": 6, "windowDurationMins": 10080, "resetsAt": 1793544194}, "secondary": None,
    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"}, "individualLimit": None,
    "spendControlReached": False, "planType": "prolite", "rateLimitReachedType": None},
    "rateLimitsByLimitId": {"codex": {
        "limitId": "codex", "limitName": None, "primary": {"usedPercent": 6, "windowDurationMins": 10080, "resetsAt": 1793544194},
        "secondary": None, "spendControlReached": False, "planType": "prolite", "rateLimitReachedType": None}},
    "rateLimitResetCredits": {"availableCount": 0, "credits": []}}

# The same account's last rollout event of the old window (2026-10-25T02:31Z, "plus"-style 7-day window due Fri 30 Oct 19:42 EET) and the first of the new one.
OLD = {"usedPercent": 84, "windowDurationMins": 10080, "resetsAt": 1793382120}
NEW_RESET = 1793544194
NOW = 1793000000          # 2026-10-26, before both resets


def with_res(res, S=None, st=None, t=NOW):
    lane = route.Lane("OPENAI_SUBSCRIPTION", "openai", "subscription")
    route.update_state(lambda s_: setattr(s_, "codex_limits", UsageCache(at=t, data=res)))
    S = route.settings_of(CFG) if S is None else route.settings_of({**CFG, "settings": S})
    route.read_codex(lane, S, st if st is not None else route.load_state(), t)
    return lane


class Fresh(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("DELEGATE_CONFIG_DIR")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DELEGATE_CONFIG_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ["DELEGATE_CONFIG_DIR"] = self.saved
        self.tmp.cleanup()


class CodexRead(Fresh):
    def test_recorded_response_is_the_weekly_window(self):
        lane = with_res(RECORDED)
        self.assertEqual((lane.five, lane.week, lane.week_reset), (None, 6.0, NEW_RESET))
        self.assertEqual([(w["name"], w["used"]) for w in lane.windows], [("weekly", 6.0)])        # one bucket, one window: the API shows no other limit
        self.assertEqual(lane.warn, [])

    def test_every_bucket_and_window_is_shown_and_the_fullest_one_binds(self):
        res = copy.deepcopy(RECORDED)
        res["rateLimits"]["secondary"] = {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": NOW + 3600}
        res["rateLimitsByLimitId"]["codex_spark"] = {"limitId": "codex_spark", "limitName": "spark",
                                                     "primary": {"usedPercent": 85, "windowDurationMins": 10080, "resetsAt": NOW + 4 * 86400}, "secondary": None}
        lane = with_res(res)
        self.assertEqual(sorted(w["name"] for w in lane.windows), ["5-hour", "spark weekly", "weekly"])
        self.assertEqual((lane.five, lane.week, lane.week_reset), (30.0, 85.0, NOW + 4 * 86400))

    def test_cockpit_prints_every_window_and_the_warning(self):
        res = copy.deepcopy(RECORDED)
        res["rateLimitsByLimitId"]["codex_spark"] = {"limitId": "codex_spark", "limitName": "spark",
                                                     "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": NOW + 86400}, "secondary": None}
        lane = with_res(res)
        cfg = copy.deepcopy(CFG)
        v = route.lane_view("OPENAI_SUBSCRIPTION", cfg, lane, support.doc_of({}), NOW, (), 0, (True, "ok", NOW), None)
        ck = {"at": NOW, "next_worker": {"action": "stop", "say": "x"}, "legend": {}, "lanes": [v]}
        text = "\n".join(route.cockpit_text(ck))
        self.assertIn("spark weekly", text)
        self.assertIn("6% used, resets Sun 01 Nov", text)


class ClaudeLast(Fresh):
    def grok(self, **over):
        g = route.Lane("GROK_SUBSCRIPTION", "xai", "subscription")
        for k, v in over.items():
            setattr(g, k, v)
        return g

    def everything(self, **over):
        ls = lanes()
        ls["GROK_SUBSCRIPTION"] = self.grok()
        for fam, name in (("deepseek", "DEEPSEEK_FREE"), ("zai", "ZAI_CODING_SUBSCRIPTION")):
            ls[name] = route.Lane(name, fam, "subscription")
        return ls

    def cfg(self):
        c = copy.deepcopy(CFG)
        c["settings"].update(deepseek_free_enabled=True, zai_coding_enabled=True)
        return c

    def pick(self, role, ls, **args):
        a = {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False}
        a.update(args)
        return support.decide(role, self.cfg(), ls, {}, T, a)

    def test_all_lanes_up_a_non_claude_lane_is_picked_for_every_role(self):
        for role in CFG["roles"]:
            for who in ([None] if role != "reviewer" else [None, "openai", "claude", "xai", "deepseek", "zai"]):
                d = self.pick(role, self.everything(), authored_by=who)
                self.assertEqual(d["action"], "run", (role, who))
                self.assertNotIn("CLAUDE", d["lane"], (role, who, d["lane"]))

    def test_the_chain_never_puts_a_claude_step_before_a_non_claude_one(self):
        for role_name, role in CFG["roles"].items():
            for who in ([None] if role_name != "reviewer" else list(role["by_author"])):
                tiers = support.build_tiers(role_name, self.cfg(), {"authored_by": who})
                flat = [s["on"] == "claude" for t in tiers for s in t]
                self.assertEqual(flat, sorted(flat), (role_name, who))

    def test_claude_is_picked_only_when_every_other_lane_is_unusable(self):
        out = {"out": True, "out_until": T + 3600, "out_reason": "limit"}
        ls = self.everything()
        for n in ("OPENAI_SUBSCRIPTION", "GROK_SUBSCRIPTION", "DEEPSEEK_FREE", "ZAI_CODING_SUBSCRIPTION"):
            ls[n].out, ls[n].out_until, ls[n].out_reason = True, T + 3600, "limit"
        for role in ("worker", "light", "planner", "hard"):
            d = self.pick(role, copy.deepcopy(ls))
            self.assertEqual((d["action"], d["lane"].startswith("CLAUDE")), ("run", True), role)
        ls["ZAI_CODING_SUBSCRIPTION"].out = False                                # one non-Claude lane back: it wins again
        self.assertEqual(self.pick("worker", ls)["lane"], "ZAI_CODING_SUBSCRIPTION")

    def test_a_relaxed_non_claude_lane_beats_a_strict_claude_lane(self):
        ls = self.everything()
        for n in ("GROK_SUBSCRIPTION", "DEEPSEEK_FREE", "ZAI_CODING_SUBSCRIPTION"):
            ls[n].out, ls[n].out_until = True, T + 3600
        ls["OPENAI_SUBSCRIPTION"].week = 96                                      # over the strict gate (95), under the ceiling (98)
        d = self.pick("worker", ls)
        self.assertEqual((d["lane"], d["pass"]), ("OPENAI_SUBSCRIPTION", "relaxed"))

    def test_same_family_review_by_a_non_claude_lane_still_precedes_claude(self):
        ls = self.everything()
        for n in ("GROK_SUBSCRIPTION", "DEEPSEEK_FREE", "ZAI_CODING_SUBSCRIPTION"):
            ls[n].out, ls[n].out_until = True, T + 3600
        d = self.pick("reviewer", ls, authored_by="openai")
        self.assertEqual((d["lane"], d["flag"]), ("OPENAI_SUBSCRIPTION", "same_family"))

    def test_premium_claude_still_goes_first_when_asked(self):
        self.assertEqual(self.pick("worker", self.everything(), premium="claude")["model"], "fable-5.1")

    def test_cockpit_next_line_says_why_claude_was_picked(self):
        cfg = self.cfg()
        ls = self.everything()
        for n in ("OPENAI_SUBSCRIPTION", "GROK_SUBSCRIPTION", "DEEPSEEK_FREE"):
            ls[n].out, ls[n].out_until = True, T + 3600
        del ls["ZAI_CODING_SUBSCRIPTION"]
        nw = route.next_worker(cfg, ls, support.doc_of({}), T)
        self.assertTrue(nw["lane"].startswith("CLAUDE"))
        for part in ("OPENAI_SUBSCRIPTION out until", "GROK_SUBSCRIPTION out until", "DEEPSEEK_FREE out until", "ZAI_CODING_SUBSCRIPTION not set up"):
            self.assertIn(part, nw["why"])
        v = route.lane_view("OPENAI_SUBSCRIPTION", cfg, None, support.doc_of({}), T)
        text = route.cockpit_text({"at": T, "next_worker": nw, "legend": {}, "lanes": [v]})[0]
        self.assertIn("(Claude because OPENAI_SUBSCRIPTION out until", text)

    def test_cockpit_next_line_has_no_reason_when_a_non_claude_lane_is_picked(self):
        nw = route.next_worker(self.cfg(), self.everything(), support.doc_of({}), T)
        self.assertEqual((nw["lane"], nw["why"]), ("DEEPSEEK_FREE", None))


if __name__ == "__main__":
    unittest.main()
