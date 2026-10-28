"""Owner holds: record, back_at, stale asks. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import json
import sys
import tempfile
import time
from unittest import mock
import unittest
from pathlib import Path
from test_route import route, CFG, T, argparse_ns, lanes
from delegate.app.supervisor import RunHooks
import routing_support as support


class Holds(unittest.TestCase):
    """delegate-dns: an owner hold {lane, roles, set_by, set_at, until (mandatory), reason}."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(route, "config_dir", return_value=Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def main(self, *argv):
        import io
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            code = route.main(list(argv))
            return code, json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = real

    def test_a_hold_without_until_is_refused(self):
        with self.assertRaises(ValueError):
            support.add_hold("OPENAI_SUBSCRIPTION", None, "pause")
        code, out = self.main("hold", "OPENAI_SUBSCRIPTION", "--reason", "pause")
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("--until", out["error"])
        self.assertEqual(route.load_state().holds, {})
        for bad in ("yesterday", "1000"):
            self.assertEqual(self.main("hold", "OPENAI_SUBSCRIPTION", "--until", bad)[0], 1)
        self.assertEqual(self.main("hold", "NOPE", "--until", "+2h")[0], 1)
        self.assertEqual(route.load_state().holds, {})
        code, out = self.main("hold", "OPENAI_SUBSCRIPTION", "--until", str(int(time.time()) + 3600), "--reason", "epoch as the command line gives it")
        self.assertEqual((code, out["ok"]), (0, True))
        self.assertEqual(self.main("hold", "OPENAI_SUBSCRIPTION", "--until", "2099-01-01T00:00:00+00:00")[0], 0)
        self.assertEqual(len(route.load_state().holds), 2)

    def test_a_refusal_says_why_there_is_no_transcript_and_what_held_the_lane(self):
        support.add_hold("OPENAI_SUBSCRIPTION", "+1h", "x")
        a = argparse_ns("worker", call_id="c#9", task=None, parent=None, title=None, brief=None, timeout=5, unattended=True, schema=None, resume="old")
        import io
        with support.fleet_patches(lanes()), mock.patch.object(route, "interactive", return_value=False), mock.patch("sys.stdout", io.StringIO()) as out:
            route.update_state(lambda s: s.refs.__setitem__("old", route.RefRecord(lane="OPENAI_SUBSCRIPTION", model="gpt-6.1-sol", mode="write", effort="medium")))
            route.run_once(a, copy.deepcopy(CFG))
        r = json.loads(out.getvalue())
        self.assertEqual((r["transcript_status"], r["call_id"], r["stop"]["reason"]), ("not_launched", "c#9", "held"))
        self.assertIsNone(route.schema_problem(r, route.classify.CONTRACT["answer_schema"]))

    def test_a_hold_stops_its_roles_until_it_ends_and_the_trace_says_who(self):
        h = support.add_hold("OPENAI_SUBSCRIPTION", "+2h", "weekly budget", ["worker", "hard"], t=T)
        self.assertEqual((h.set_by, h.until, h.roles), ("owner", T + 7200, ["hard", "worker"]))
        st = route.load_state()
        d = support.decide("worker", copy.deepcopy(CFG), lanes(), st, T, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False})
        self.assertNotEqual(d["lane"], "OPENAI_SUBSCRIPTION")
        self.assertTrue(any(x.startswith("OPENAI_SUBSCRIPTION: rejected, held: owner until") and "weekly budget" in x for x in d["trace"]), d["trace"])
        p = support.decide("planner", copy.deepcopy(CFG), lanes(), st, T, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False})
        self.assertEqual(p["lane"], "OPENAI_SUBSCRIPTION")        # the hold names roles
        later = support.decide("worker", copy.deepcopy(CFG), lanes(), st, T + 7201, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False})
        self.assertEqual(later["lane"], "OPENAI_SUBSCRIPTION")      # and ends by itself

    def test_a_hold_with_no_roles_stops_the_lane_for_every_role(self):
        support.add_hold("OPENAI_SUBSCRIPTION", "+1h", "", t=T)
        for role in ("planner", "worker", "reviewer"):
            d = support.decide(role, copy.deepcopy(CFG), lanes(), route.load_state(), T, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False})
            self.assertNotEqual(d.get("lane"), "OPENAI_SUBSCRIPTION", role)

    def test_refusals_under_a_hold_wait_for_its_end_never_one_second(self):
        h = support.add_hold("OPENAI_SUBSCRIPTION", "+3h", "pause", ["worker"], t=T)
        a = argparse_ns("worker", resume="old")
        st = support.doc_of({})
        st.holds[h.id] = h
        st.refs["old"] = route.RefRecord(lane="OPENAI_SUBSCRIPTION", model="gpt-6.1-sol", mode="write", effort="medium")
        with mock.patch.object(route, "load_state", return_value=st), support.fleet_patches(lanes()), mock.patch.object(route, "now", return_value=T):
            d, _, _, _ = route.decision(a, copy.deepcopy(CFG))
        self.assertEqual((d["action"], d["retry_after"], d["outcome"]), ("stop", h.until, "held"))
        self.assertIn("owner", d["say"])
        res = route.launch_worker({"role": "worker", "lane": "OPENAI_SUBSCRIPTION", "how": {"kind": "codex", "sandbox": "workspace-write"}}, "b", argparse_ns("worker"), "rid", T, CFG, {}, RunHooks())
        self.assertEqual((res["retry_after"], res["outcome"]), (h.until, "held"))

    def test_release_and_status_show_the_holder_and_expiry(self):
        h = support.add_hold("OPENAI_SUBSCRIPTION", "+2d", "budget", ["worker"], "alice")
        with support.fleet_patches(lanes()):
            _, st = self.main("status", "--json")
        row = st["holds"][0]
        self.assertEqual((row["id"], row["set_by"], row["roles"], row["reason"]), (h.id, "alice", ["worker"], "budget"))
        self.assertGreater(row["until"], time.time())
        self.assertEqual(self.main("release", h.id)[0], 0)
        self.assertEqual(self.main("holds")[1]["holds"], [])
        self.assertEqual(self.main("release", "nothing")[0], 1)


class BackAt(unittest.TestCase):
    def test_a_lane_over_its_ceiling_is_back_at_that_windows_reset(self):
        S = route.settings_of(route.load_routing())
        l = route.Lane("OPENAI_SUBSCRIPTION", "openai", "subscription")
        l.five, l.five_reset, l.week, l.week_reset = 99, 1000.0, 10, 9000.0
        self.assertEqual(l.back_at(S), 1000.0)
        l.five, l.week = 10, 99
        self.assertEqual(l.back_at(S), 9000.0)
        l.week = 10
        self.assertIsNone(l.back_at(S))
        l.out, l.out_until = True, 500.0
        self.assertEqual(l.back_at(S), 500.0)


class StaleHolds(unittest.TestCase):
    """delegate-eau: a hold whose lane has gone quiet raises ONE ask (a status field)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(route, "config_dir", return_value=Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def lanes(self, week):
        l = route.Lane("OPENAI_SUBSCRIPTION", "openai", "subscription")
        l.week, l.five = week, 3
        return {l.name: l}

    def test_one_ask_for_a_stale_hold_never_a_second_and_nothing_is_printed(self):
        import io
        h = support.add_hold("OPENAI_SUBSCRIPTION", "+2d", "weekly spent")
        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            asks, st = route.settle_holds(route.states(), self.lanes(80), route.load_state(), route.now())
            self.assertEqual(asks, [])                                       # the lane is still full: the hold is doing its job
            asks, st = route.settle_holds(route.states(), self.lanes(0), route.load_state(), route.now())
            asks2, st2 = route.settle_holds(route.states(), self.lanes(0), route.load_state(), route.now() + 600)
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = real
        self.assertEqual(printed, "")
        asks, asks2 = [a.as_dict() for a in asks], [a.as_dict() for a in asks2]
        self.assertEqual([(a["kind"], a["id"], a["lane"]) for a in asks], [("hold_stale", h.id, "OPENAI_SUBSCRIPTION")])
        self.assertEqual(asks2, asks)                                        # the same ask (same id, same raised_at), not a new one
        self.assertEqual(st2.holds[h.id].stale_at, asks[0]["raised_at"])
        self.assertIn("route.py release " + h.id, asks[0]["answer"])
        self.assertTrue(route.HoldBook(st2.holds).rows(route.now())[0]["stale"])

    def test_status_carries_asks(self):
        payload = route.status_payload(CFG, self.lanes(50), [], route.load_state(), route.now())
        self.assertEqual(payload["asks"], [])
        self.assertIn("health", payload)


if __name__ == "__main__":
    unittest.main()
