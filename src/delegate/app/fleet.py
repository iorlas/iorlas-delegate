"""FleetBuilder: every `Lane` for one call. Gathers what the router needs and nothing else: the lane profile (which lanes this machine uses),
a usage reading per lane, the out board, the health fold and the leases. Side effects go through the injected stores and readers.

`mark_busy`, `settle_holds` and `drift_warnings` are the three things a call does to the fleet before the Router sees it.
"""
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from delegate.contracts.history import HealthState
from delegate.contracts.routing import LaneSpec, ModelSpec, Routing, Settings
from delegate.contracts.state import AuthEntry, StateDoc
from delegate.contracts.stores import CheckpointStore, ConfigStore, HistoryStore, StateStore
from delegate.domain.health.ledger import HealthLedger
from delegate.domain.holds.book import HoldBook, StaleHold
from delegate.domain.lanes.lane import Lane
from delegate.domain.lanes.leases import LeaseBook

Reader = Callable[[Lane, Settings, StateDoc, float, object], None]      # fills a lane's usage in place (the claude reader takes the caller's get_usage)
DRIFT_NOTE = "enabled but not ready"
WARN_EVERY_S = 86400


@dataclass
class Fleet:
    lanes: dict[str, Lane]
    offers: list[str] = field(default_factory=list)       # lanes seen for the first time and not usable: reported once, then remembered


class FleetBuilder:
    def __init__(self, routing: Routing, states: StateStore, history: HistoryStore, checkpoints: CheckpointStore, config: ConfigStore,
                 readers: Mapping[str, Reader], configured: Callable[[], dict[str, bool]]) -> None:
        self.r, self.states, self.history, self.checkpoints, self.config = routing, states, history, checkpoints, config
        self.readers, self.configured = readers, configured

    def build(self, doc: StateDoc, t: float, usage: object = None, check_config: bool = True) -> Fleet:
        s = self.r.settings
        fleet = Fleet({})
        conf = self.configured() if check_config else {}
        health = self._health(t)
        for name, spec in self.r.lanes.items():
            if (name == "DEEPSEEK_FREE" and not s.deepseek_free_enabled) or (name == "ZAI_CODING_SUBSCRIPTION" and not s.zai_coding_enabled):
                continue
            mode = self._mode(name, spec, conf.get(name, True), fleet) if check_config else "on"
            if mode == "off":
                continue
            lane = Lane(name, spec.family, spec.kind, spec.shared_with_orchestrator)
            self.readers[spec.reader](lane, s, doc, t, usage)
            self._apply_out(lane, doc, t)
            if mode == "drift":
                lane.out, lane.out_until, lane.out_reason = True, None, "logged_out"
                lane.notes.insert(0, DRIFT_NOTE + " (login or key gone?)")
            apply_health(lane, self.r.models, HealthLedger(s.breaker), health, t)
            fleet.lanes[name] = lane
        return fleet

    def _mode(self, name: str, spec: LaneSpec, ready: bool, fleet: Fleet) -> str:
        """-> "on" | "off" | "drift". Shared lanes are always on. Otherwise the profile decides; a lane never decided is turned on once if
        the machine has it ready, else left out silently."""
        if spec.shared_with_orchestrator:
            return "on"
        prof = self.config.profile()
        v = prof.lanes.get(name)
        if v == "declined":
            return "off"
        if v == "enabled":
            return "on" if ready else "drift"
        if ready:
            self.config.set_lane(name, "enabled")
            return "on"
        if name not in prof.offered and name not in fleet.offers:
            fleet.offers.append(name)
        return "off"

    def _apply_out(self, lane: Lane, doc: StateDoc, t: float) -> None:
        o = doc.out.get(lane.name)
        if o and o.reason == "limit" and lane.read_at and lane.read_at > o.at and not lane.out and (lane.five or 0) < 95 and (lane.week or 0) < 95:
            self.states.update(lambda s: s.out.pop(lane.name, None))       # a reading after the refusal shows room: the out was wrong or is over
            o = None
        if o and o.until > t and not lane.out:      # an out reported by the model or by a launcher
            lane.out, lane.out_until, lane.out_reason = True, o.until, o.reason
            lane.notes.append(o.why)

    def _health(self, t: float) -> HealthState:
        """The health fold over history.jsonl, continued from a cached checkpoint (a cache: no file, a damaged one or another policy = a full fold)."""
        pol = self.r.settings.breaker
        base = self.checkpoints.load(pol)
        rows = self.history.rows(base.ts if base else 0.0)
        state = HealthLedger(pol).fold(rows, base)
        if rows:
            self.checkpoints.save(state, pol)
        return state


def apply_health(lane: Lane, models: Mapping[str, ModelSpec], ledger: HealthLedger, state: HealthState, t: float) -> None:
    """Put the breaker's view of each (lane, model) pair on the lane: `benched` (refused), `canary` (the next launch is the probe), `hview`."""
    for model, m in models.items():
        if m.family != lane.family:
            continue
        v = ledger.view(state, lane.name, model, t)
        lane.hview[model] = v
        if v.state == "broken":
            lane.benched[model] = v
        elif v.canary:
            lane.canary[model] = True


def mark_busy(lanes: dict[str, Lane], doc: StateDoc, s: Settings, t: float, jitter: Callable[[], int]) -> dict[str, Lane]:
    """A lane with all its slots taken is skipped like a refused lane (a copy that is out); `jitter` keeps retrying engines from arriving together."""
    book, out = LeaseBook(doc.leases), {}
    for name, lane in lanes.items():
        cap, running = lane.slots(s), book.running(name, t)
        out[name] = lane.with_busy(t + jitter(), running, cap) if cap is not None and running >= cap and not lane.out else lane
    return out


def settle_holds(states: StateStore, lanes: dict[str, Lane], doc: StateDoc, t: float) -> tuple[list[StaleHold], StateDoc]:
    """A hold in force whose lane shows less than HOLD_STALE_BELOW % used is stale: it gets ONE ask, stamped on the record (`stale_at`) so no
    later call raises it again. -> (the asks, the state after)."""
    used = {n: lane.week if lane.week is not None else lane.five for n, lane in lanes.items()}
    stamp = HoldBook(doc.holds).to_stamp(used, t)
    if stamp:
        def apply(s: StateDoc) -> None:
            for k in stamp:
                h = s.holds.get(k)
                if h and not h.stale_at:
                    h.stale_at = t
        doc = states.update(apply)
    return HoldBook(doc.holds).asks(used, t), doc


def drift_warnings(states: StateStore, lanes: dict[str, Lane], doc: StateDoc, t: float) -> list[str]:
    """A lane the profile enabled but the machine no longer supports: say so once a day."""
    bad = [n for n, lane in lanes.items() if lane.notes and lane.notes[0].startswith(DRIFT_NOTE)]
    out = [f"{n} is enabled but not ready, so it is skipped (route.py doctor; or route.py doctor --decline {n})"
           for n in bad if t - doc.warned_at.get(n, 0) > WARN_EVERY_S]
    if out:
        states.update(lambda s: s.warned_at.update({n: t for n in bad}))
    return out




class AuthCache:
    """The one login cache (state.json `auth`): a login probe spawns a CLI, so its answer is kept. Readers pass the age they accept."""

    def __init__(self, states: StateStore, use: bool = True) -> None:
        self.states, self.use = states, use

    def fresh(self, name: str, t: float, ttl: float, doc: StateDoc | None = None) -> AuthEntry | None:
        e = (doc or self.states.load()).auth.get(name)
        return e if self.use and e and 0 <= t - e.at <= ttl else None

    def remember(self, name: str, ok: bool | None, why: str, t: float) -> None:
        """Never fails the caller; an unknown answer (None) is not remembered."""
        if ok is not None:
            with contextlib.suppress(Exception):
                self.states.update(lambda s: s.auth.__setitem__(name, AuthEntry(at=t, ok=bool(ok), why=str(why)[:120])))

    def rows(self, doc: StateDoc, t: float) -> dict[str, dict]:
        return {n: {"ok": e.ok, "why": e.why, "age_s": int(t - e.at)} for n, e in doc.auth.items()}
