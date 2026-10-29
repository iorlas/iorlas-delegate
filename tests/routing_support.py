"""Test helpers for the tests that drive route.py's callers of the domain Router: a `decide` with the shape the old route.decide had
(role, cfg, lanes, state, t, args -> the JSON answer of `next`, `how` included) and writers for state sections."""
import contextlib
import copy
import sys
import types
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from delegate.app import cockpit, decide, env
from delegate.app import decide as dec
from delegate.app.fleet import Fleet
from delegate.contracts import codec
from delegate.contracts.routing import CallArgs, Run
from delegate.contracts.state import Hold, Lease, OutState, StateDoc
from delegate.domain.holds.book import HoldBook, new_hold
from delegate.domain.routing.router import Router
from delegate.domain.routing.tiers import TierPlan


def doc_of(st):
    """A StateDoc from a StateDoc, or from a dict of sections in their state.json shape."""
    return st if isinstance(st, StateDoc) else codec.from_json(StateDoc, dict(st or {}, v=1))


def decide(role, cfg, ls, st, t, args):
    routing = env.typed_routing(cfg)
    doc = doc_of(st)
    ca = CallArgs(authored_by=args.get("authored_by"), premium=args.get("premium"),
                  allow_degraded=bool(args.get("allow_degraded")), unattended=bool(args.get("unattended")),
                  engine_only=tuple(args.get("engine_only") or ()))
    d = Router(routing, HoldBook(doc.holds), doc.prev, t).decide(role, ca, ls, wait_busy=bool(args.get("wait_busy")))
    out = d.as_dict()
    if isinstance(d, Run):
        step = {"model": d.model, "effort": d.effort, "mode": d.mode}
        if d.cap is not None:
            step["cap"] = d.cap
        out["how"] = dec.how(ls[d.lane], step, cfg, args.get("cwd"), args.get("unattended", False))
    return out


def router(cfg, t, st=None):
    doc = doc_of(st or {})
    return Router(env.typed_routing(cfg), HoldBook(doc.holds), doc.prev, t)


def build_tiers(role_name, cfg, args, off=frozenset()):
    """The role's tiers for `args` (a dict as `next` takes), as dicts: what the old route.build_tiers gave."""
    routing = env.typed_routing(cfg)
    ca = CallArgs(authored_by=args.get("authored_by"))
    tiers = TierPlan(routing).tiers(routing.roles[role_name], ca, off)
    return [[codec.to_json(s) for s in t] for t in tiers or []]


def put_out(lane, until, why="t", **kw):
    env.update_state(lambda s: s.out.__setitem__(lane, OutState(until=until, why=why, **kw)))


def put_lease(rid, **kw):
    kw.setdefault("pid", 1)
    env.update_state(lambda s: s.leases.__setitem__(rid, Lease(**kw)))


def lease(**kw):
    kw.setdefault("pid", 1)
    return Lease(**kw)


def put_hold(h):
    env.update_state(lambda s: s.holds.__setitem__(h.id, h))


def holds_of(*hs):
    return {h.id: h for h in hs}


def clear_out():
    env.update_state(lambda s: s.out.clear())


def state_json():
    return codec.to_json(env.load_state())


__all__ = ["Hold", "add_hold", "clear_out", "copy", "decide", "fleet_patches", "holds_of", "lease", "put_hold", "put_lease", "put_out", "state_json"]


def add_hold(lane, until, reason, roles=None, set_by="owner", t=None):
    """Record an owner hold the way `route.py hold` does; ValueError for an end that is missing or past."""
    h = new_hold(uuid.uuid4().hex[:8], lane, until, reason, list(roles or []), set_by, env.now() if t is None else t)
    env.update_state(lambda s: s.holds.__setitem__(h.id, h))
    return h


@contextlib.contextmanager
def fleet_patches(ls):
    """The fleet is `ls`: no reader, no profile, no health fold, busy marking and lane publishing off."""
    builder = types.SimpleNamespace(build=lambda *a, **k: Fleet(ls))
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(dec, "fleet_builder", return_value=builder))
        stack.enter_context(mock.patch.object(dec, "publish_lanes"))
        for m in (dec, cockpit):
            stack.enter_context(mock.patch.object(m, "mark_busy", side_effect=lambda lanes, *a, **k: lanes))
        yield
