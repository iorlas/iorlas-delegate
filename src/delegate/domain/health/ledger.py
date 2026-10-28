"""HealthLedger: lane health as a replayable fold over history rows (delegate-rdt, delegate-a9x).

Pure: no clock, no I/O. `fold(rows, state)` turns finished-run rows into a `HealthState`; `view(state, lane, model, t)` answers "may this
(lane, model) pair launch at time t, and if not why". The same rows replayed in time order give the same answer, so a past incident is a
regression fixture (tests/domain/test_health.py, golden/health_replay.json).

Two kinds of fact, both read from the run's outcome:
  lane facts   rate_limited, credit_exhausted, logged_out: the whole lane is out until a time (the vendor's, else a probe time).
  pair facts   vendor_blip, worker_failed, hung: counted per fingerprint per (lane, model) inside a window; enough of them bench the pair on
               a 15/30/60 min ladder. A launch after a bench ends is the canary: it passes (a `done`) and the ladder resets, or it fails and
               the next rung starts at once.
Never counted: bad_request (the caller's fault), network, killed, timeout, secrets, schema_invalid, refusals.
"""
from dataclasses import dataclass

from delegate.contracts.history import (CanaryRow, Failure, HealthState, HistoryRow, LaneFact, PairFact, ReleaseRow,
                                        RunRow, Streak)
from delegate.contracts.routing import BreakerPolicy
from delegate.domain.health.matchers import fingerprint

LANE_KINDS = ("rate_limited", "credit_exhausted", "logged_out")
BREAKER = {"vendor_blip": {"tasks": 2}, "worker_failed": {"tasks": 3}, "hung": {"n": 2, "window_min": 30, "tasks": 1}}
STATES = ("up", "busy", "exhausted", "logged_out", "held", "broken")
RELEASE, DONE = "release", "done"


def failure_fields(outcome: str | None, source: str, say: str | None, until: float | None = None) -> dict:
    """-> the health fields a run or refusal row carries; {} for a done run (outcome None)."""
    if not outcome:
        return {}
    return {"outcome": outcome, "fingerprint": fingerprint(outcome, source, say), "say": (say or "")[:240], "until": until}


@dataclass(frozen=True)
class PairView:
    """One (lane, model) pair at one time. `usable` is False while a bench or a lane-level out is running; once it ends the pair is `up`
    with canary True: the next launch is the one probe before full traffic."""

    state: str = "up"
    usable: bool = True
    canary: bool = False
    until: float | None = None
    source: str | None = None
    kind: str | None = None
    evidence: str = ""
    rung: int = 0
    last_failures: tuple[Failure, ...] = ()

    def as_dict(self) -> dict:
        return {"state": self.state, "usable": self.usable, "canary": self.canary, "until": self.until, "source": self.source,
                "kind": self.kind, "evidence": self.evidence, "rung": self.rung,
                "last_failures": [{"ts": f.ts, "outcome": f.outcome, "say": f.say} for f in self.last_failures]}


@dataclass(frozen=True)
class Fact:
    """A history row read as a health fact: what happened (done | release | an outcome name) to which pair, when."""

    what: str
    ts: float
    lane: str
    model: str | None = None
    until: float | None = None
    say: str = ""
    fingerprint: str | None = None
    task: str | None = None


def fact_of(row: HistoryRow) -> Fact | None:
    """None when the row is not a fact: a refusal, a gap, a reap, a caller fault, network, killed, timeout..."""
    if isinstance(row, ReleaseRow):
        return Fact(RELEASE, row.ts, row.lane)
    if isinstance(row, CanaryRow):
        return Fact(DONE, row.ts, row.lane, row.model)
    if not isinstance(row, RunRow):
        return None
    if row.done:
        return Fact(DONE, row.ts, row.lane, row.model)
    o = row.outcome
    if o in LANE_KINDS or o in BREAKER:
        return Fact(o, row.ts, row.lane, row.model, row.until, row.say or "", row.fingerprint, row.task or row.run_id)
    return None


class HealthLedger:
    """The fold, with the breaker policy it runs under."""

    def __init__(self, policy: BreakerPolicy) -> None:
        self.pol = policy

    def step(self, state: HealthState, row: HistoryRow) -> None:
        """Apply one history row to `state` (mutates). Rows must come in time order."""
        state.ts = max(state.ts, row.ts)
        f = fact_of(row)
        if f is not None:
            self._apply(state, f)

    def _apply(self, state: HealthState, f: Fact) -> None:
        if f.what == RELEASE:       # `route.py report --event ok`: the owner says the lane is fine
            state.lanes.pop(f.lane, None)
            for k in [k for k in state.pairs if k.startswith(f.lane + "|")]:
                state.pairs.pop(k)
        elif f.what == DONE:        # a run that worked proves the lane and resets the ladder of its pair
            state.lanes.pop(f.lane, None)
            p = state.pairs.setdefault(f"{f.lane}|{f.model}", PairFact())
            p.streak, p.rung, p.until, p.kind, p.fp, p.evidence, p.source, p.last_done = [], 0, 0.0, None, None, "", None, f.ts
        elif f.what in LANE_KINDS:
            state.lanes[f.lane] = self._lane_fact(f)
        else:
            self._pair_fact(state.pairs.setdefault(f"{f.lane}|{f.model}", PairFact()), f)

    def _lane_fact(self, f: Fact) -> LaneFact:
        pol = self.pol
        if f.what == "credit_exhausted":      # a spending limit has no timer: look again after the probe time
            until, src = f.ts + pol.credit_probe_s, "probe"
        elif f.what == "logged_out":
            until, src = f.ts + pol.logout_s, "probe"
        else:
            src = "vendor" if f.until is not None and f.until > f.ts else "guess"
            until = f.until if src == "vendor" else f.ts + pol.guess_s
        return LaneFact(kind=f.what, until=float(until), source=src, evidence=f.say[:240], ts=f.ts)

    def _pair_fact(self, p: PairFact, f: Fact) -> None:
        pol, ts = self.pol, f.ts
        p.fails = (p.fails + [Failure(ts=ts, outcome=f.what, say=f.say[:120])])[-3:]
        if ts < p.until:        # launched while benched (a call already in flight): it does not count again
            return
        rule = BREAKER[f.what]
        n, win = rule.get("n", pol.n), rule.get("window_min", pol.window_min)
        fp = f.fingerprint or fingerprint(f.what, "", f.say)
        probation = p.rung > 0 and ts >= p.until           # the canary failed: the next rung starts at once
        p.streak = [s for s in p.streak if ts - s.ts <= win * 60 and s.outcome == f.what] + [Streak(ts=ts, outcome=f.what, fp=fp, task=f.task)]
        same = [s for s in p.streak if s.fp == fp]
        if probation or (len(same) >= n and len({s.task for s in same}) >= rule["tasks"]):
            rung = min(p.rung + 1, len(pol.ladder_min))
            p.rung, p.until, p.kind, p.fp, p.evidence, p.source, p.streak = rung, ts + pol.ladder_min[rung - 1] * 60, f.what, fp, f.say[:240], "breaker", []

    def fold(self, rows: list[HistoryRow], state: HealthState | None = None) -> HealthState:
        """The state after `rows` on top of `state` (a checkpoint, else empty). Rows at or before the checkpoint's ts are skipped, so
        fold(all) == fold(tail, fold(head)): the checkpoint is a cache, never truth."""
        state = state if state is not None else HealthState()
        for r in sorted((r for r in rows if r.ts > state.ts), key=lambda r: r.ts):
            self.step(state, r)
        return state

    def view(self, state: HealthState, lane: str, model: str, t: float) -> PairView:
        L, p = state.lanes.get(lane), state.pairs.get(f"{lane}|{model}")
        fails = tuple(p.fails) if p else ()
        rung = p.rung if p else 0
        if L:
            if t < L.until:
                return PairView(state="logged_out" if L.kind == "logged_out" else "exhausted", usable=False, until=L.until, source=L.source,
                                kind=L.kind, evidence=L.evidence, rung=rung, last_failures=fails)
        canary, kind, evidence = bool(L), L.kind if L else None, L.evidence if L else ""
        if p and p.rung:
            if t < p.until:
                return PairView(state="broken", usable=False, canary=bool(L), until=p.until, source=p.source, kind=p.kind,
                                evidence=p.evidence, rung=rung, last_failures=fails)
            canary, kind, evidence = True, p.kind, p.evidence
        return PairView(canary=canary, kind=kind, evidence=evidence, rung=rung, last_failures=fails)


def combine(v: PairView, held: bool = False, busy: bool = False) -> str:
    """-> the one state word of a pair: held > logged_out > exhausted > broken > busy > up. Holds and leases are not history facts."""
    if held:
        return "held"
    if v.state in ("logged_out", "exhausted", "broken"):
        return v.state
    return "busy" if busy else "up"


def stale_usage(read_at: float | None, t: float, policy: BreakerPolicy) -> float | None:
    """-> hours old when a usage reading is older than the stale limit (it is then unknown, never a silent gate), else None."""
    return round((t - read_at) / 3600, 1) if read_at and t - read_at > policy.stale_usage_s else None
