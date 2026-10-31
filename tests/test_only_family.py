"""delegate-gsy: `run ROLE --only-family F` keeps only the role's steps of those families, same order, with no fallback outside them."""
import copy
import unittest

import hermetic  # noqa: F401  (a home of its own for every test)
import routing_support as support
from test_review_order import CFG, T, lanes

from delegate.contracts.routing import CallArgs
from delegate.domain.routing.tiers import TierPlan


class OnlyFamilyTest(unittest.TestCase):
    def decide(self, *up, role="worker", **kw):
        return support.decide(role, copy.deepcopy(CFG), lanes(*up), {}, T, dict({"authored_by": None, "unattended": True}, **kw))

    def test_the_answer_never_leaves_the_family_and_names_it(self):
        everything_up = ("openai", "xai", "claude")
        d = self.decide(*everything_up, only_family=["openai"])
        self.assertEqual((d["action"], d["lane"]), ("run", "OPENAI_SUBSCRIPTION"))

    def test_no_step_of_another_family_is_ever_planned(self):
        plan = TierPlan(support.env.typed_routing(copy.deepcopy(CFG)))
        role = plan.r.roles["worker"]
        tiers = plan.tiers(role, CallArgs(only_family=("openai",))) or []
        steps = [x for tier in tiers for x in tier]
        self.assertTrue(steps)
        self.assertEqual({x.on for x in steps}, {"openai"})
        both = [x for tier in plan.tiers(role, CallArgs(only_family=("xai", "openai"))) or [] for x in tier]
        self.assertEqual({x.on for x in both}, {"xai", "openai"})
        self.assertEqual([x.model for x in both if x.on == "openai"], [x.model for x in steps])

    def test_when_the_family_is_not_ready_the_answer_is_a_stop_with_the_reason(self):
        d = self.decide("xai", "claude", only_family=["openai"])
        self.assertEqual((d["action"], d["reason"]), ("stop", "only_family_unready"))
        self.assertNotIn("lane", d)
        self.assertIsNone(d.get("ask_user"))
        self.assertIn("--only-family openai", d["say"])
        self.assertIn("no other family is used", d["say"])

    def test_a_family_with_no_step_in_the_role_is_a_stop_too(self):
        d = self.decide("openai", "xai", "claude", only_family=["nonesuch"])
        self.assertEqual((d["action"], d["reason"]), ("stop", "only_family_unready"))

    def test_without_the_flag_the_answer_is_unchanged(self):
        for up in (("openai", "xai", "claude"), ("xai", "claude"), ("claude",), ()):
            self.assertEqual(self.decide(*up), self.decide(*up, only_family=[]), up)
            self.assertNotEqual(self.decide(*up).get("reason"), "only_family_unready", up)


if __name__ == "__main__":
    unittest.main()
