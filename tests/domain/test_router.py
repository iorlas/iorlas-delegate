"""The router is pure and replays the characterization goldens (golden/decisions.json, taken from route.py before D2 moved it)."""
import json
import socket
import sys
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from delegate.app.fleet import mark_busy
from delegate.contracts import codec
from delegate.contracts.routing import CallArgs, Error, LaneOut, Routing, Run, Stop
from delegate.contracts.state import StateDoc
from delegate.domain.health.ledger import PairView
from delegate.domain.holds.book import HoldBook
from delegate.domain.lanes.lane import Lane
from delegate.domain.routing.router import Router

HERE = Path(__file__).resolve().parent
T = 1_000_000_000.0
CFG = json.loads((ROOT / "skills" / "delegate" / "routing.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((HERE / "golden" / "decisions.json").read_text(encoding="utf-8"))


def lane_of(spec):
    lane = Lane(spec["name"], spec["family"], spec["kind"], spec["shared"])
    for k, v in spec.items():
        if k in ("benched", "hview"):
            v = {m: PairView(**{**b, "state": "broken", "usable": False}) for m, b in v.items()}
        if k not in ("name", "family", "kind", "shared"):
            setattr(lane, k, v)
    return lane


def routing(overrides):
    cfg = json.loads(json.dumps(CFG))
    cfg["settings"].update(overrides)
    return codec.from_json(Routing, cfg)


def call_args(a, unattended):
    return CallArgs(authored_by=a["authored_by"], premium=a["premium"],
                    allow_degraded=a["allow_degraded"], unattended=unattended, resume=a["resume"], cap=a["cap"])


def run_scenario(sc):
    r = routing(sc["settings"])
    doc = codec.from_json(StateDoc, dict(sc["state"], v=1))
    lanes = mark_busy({s["name"]: lane_of(s) for s in sc["lanes"]}, doc, r.settings, T, lambda: 77)
    router = Router(r, HoldBook(doc.holds), doc.prev, T)
    a = sc["args"]
    return router.route(a["role"], call_args(a, sc["unattended"]), lanes, doc.refs.get(a["resume"] or ""))


class Replay(unittest.TestCase):
    def test_every_golden_decision_is_reproduced(self):
        for sc in GOLDEN:
            with self.subTest(sc["name"]):
                self.assertEqual(run_scenario(sc).as_dict(), {k: v for k, v in sc["expect"].items() if not k.startswith("_")})


class Purity(unittest.TestCase):
    def test_decide_touches_no_filesystem_socket_or_env(self):
        r = routing({})
        lanes = {"OPENAI_SUBSCRIPTION": Lane("OPENAI_SUBSCRIPTION", "openai", "subscription", five=10, week=5)}
        router = Router(r, HoldBook({}), {}, T)
        import builtins
        import os
        real = (builtins.open, os.stat, os.listdir, socket.socket)

        def boom(*a, **k):
            raise AssertionError("the router did I/O")
        builtins.open, os.stat, os.listdir, socket.socket = boom, boom, boom, boom
        try:
            d = router.decide("worker", CallArgs(), lanes)
        finally:
            builtins.open, os.stat, os.listdir, socket.socket = real
        self.assertIsInstance(d, Run)
        self.assertEqual((d.lane, d.model), ("OPENAI_SUBSCRIPTION", "gpt-6.1-sol"))

    def test_decide_does_not_change_the_lanes_it_is_given(self):
        r = routing({})
        lanes = {"OPENAI_SUBSCRIPTION": Lane("OPENAI_SUBSCRIPTION", "openai", "subscription", five=10, week=5, out=True, out_until=T + 5)}
        before = repr(lanes)
        Router(r, HoldBook({}), {}, T).decide("worker", CallArgs(), lanes)
        self.assertEqual(repr(lanes), before)

    def test_the_decision_kinds(self):
        r = routing({})
        router = Router(r, HoldBook({}), {}, T)
        self.assertIsInstance(router.decide("nope", CallArgs(), {}), Error)
        self.assertIsInstance(router.decide("worker", CallArgs(), {}), Stop)
        self.assertIsInstance(router.resume("worker", CallArgs(resume="x"), None, {}), Error)
        self.assertIsInstance(Lane("L", "f", "subscription").with_busy(T, 1, 1), Lane)
        self.assertFalse(hasattr(Lane("L", "f", "subscription"), "held"))
        self.assertTrue(LaneOut)


if __name__ == "__main__":
    unittest.main()
