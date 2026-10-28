"""Router: the pure decision. `decide(...)` and `resume(...)` read records and return a Decision; they touch no file, clock, process or
global, and never build an argv (that is the harness's `launch_spec`). Lanes are never mutated: the holds that cover the role are looked up
in the HoldBook and handed to the picker.
"""
from dataclasses import replace

from delegate.contracts.routing import AskUser, CallArgs, Decision, Error, LaneOut, Role, Routing, Run, Step, Stop
from delegate.contracts.state import Dwell, RefRecord
from delegate.domain.holds.book import HoldBook, hold_text
from delegate.domain.lanes.lane import Lane
from delegate.domain.routing.dwell import DwellMemory
from delegate.domain.routing.picker import LanePicker
from delegate.domain.routing.tiers import LATE_FLAGS, TierPlan
from delegate.domain.time.spec import local

OUTCOME_OF_OUT = {"logged_out": "logged_out", "hung": "hung", "credit": "credit_exhausted"}


def callable_lanes(lanes: dict[str, Lane], unattended: bool) -> dict[str, Lane]:
    """No one pays API credit knowingly when the caller is an engine: credit lanes are never picked unattended."""
    return {n: lane for n, lane in lanes.items() if lane.kind != "credit"} if unattended else lanes


class Router:
    def __init__(self, routing: Routing, holds: HoldBook, prev: dict[str, Dwell], t: float) -> None:
        self.r = routing
        self.holds = holds
        self.dwell = DwellMemory(prev, routing.settings.min_dwell_minutes)
        self.t = t
        self.plan = TierPlan(routing)

    def _picker(self, lanes: dict[str, Lane], role: str | None) -> LanePicker:
        return LanePicker(self.r.settings, {n: self.holds.covering(n, role, self.t) for n in lanes}, self.dwell, self.t)

    def route(self, role: str, args: CallArgs, lanes: dict[str, Lane], rec: RefRecord | None = None) -> Decision:
        """The call's decision: `--resume` continues its thread on its lane; otherwise the role's chain. An unattended caller (an engine) has
        no one to ask, and waits for a busy first choice."""
        lanes = callable_lanes(lanes, args.unattended)
        if args.resume:
            return self.resume(role, args, rec, lanes)
        d = self.decide(role, replace(args, allow_degraded=args.allow_degraded and not args.unattended), lanes, wait_busy=args.unattended)
        return replace(d, ask_user=None) if args.unattended and isinstance(d, Stop) else d

    def decide(self, role_name: str, args: CallArgs, lanes: dict[str, Lane], wait_busy: bool = False) -> Decision:
        role = self.r.roles.get(role_name)
        if role is None:
            return Error(error=f"unknown role {role_name}; roles: {', '.join(self.r.roles)}")
        tiers = self.plan.tiers(role, args)
        if tiers is None:
            return Error(error=self._tier_error(role, args))
        s, picker, trace = self.r.settings, self._picker(lanes, role_name), []
        steps = [x for tier in tiers for x in tier]
        first = next((x for x in steps if any(lane.family == x.on for lane in lanes.values())), steps[0] if steps else None)
        mine = [lane for lane in lanes.values() if first and lane.family == first.on]
        busy = [lane for lane in mine if lane.busy and lane.usable_if_free(s)]
        if busy and wait_busy and not any(picker.usable(lane, True)[0] for lane in mine):
            until = min(lane.out_until or 0 for lane in busy)      # the first choice is only waiting for a slot: a minute beats a round on the next rung
            return Stop(reason="busy", lane=busy[0].name, retry_after=until, trace=(),
                        say=f"{busy[0].name} (the role's first choice) has no free slot: {busy[0].notes[0]}")
        why_not_first = "busy" if busy else "out" if any(lane.out for lane in mine) else "ceiling"
        first_lane = next((lane.name for lane in mine if lane.kind == "subscription"), mine[0].name if mine else None)
        for tier in tiers:
            for relaxed in (False, True):
                for step in tier:
                    lane = picker.pick(step.on, lanes, relaxed, trace, step.model)
                    if lane is None:
                        continue
                    fb = step is not first
                    return Run(role=role_name, lane=lane.name, model=step.model, effort=step.effort, mode=step.mode, cap=step.cap, flag=step.flag,
                               pass_="relaxed" if relaxed else "strict", rung=step.idx, fallback=fb, fallback_reason=why_not_first if fb else None,
                               first_lane=first_lane if fb else None, trace=tuple(trace),
                               warning=self._warning(step, args.authored_by, lanes, picker))
        return self._stop(role_name, role, tiers, args, lanes, trace)

    def _tier_error(self, role: Role, args: CallArgs) -> str:
        if args.premium and not self.r.premium.get(args.premium):
            return f"no premium model configured for family {args.premium}; premium: {[k for k, v in self.r.premium.items() if v]}"
        return f"--authored-by must be {' or '.join(k for k in (role.by_author or {}) if k != 'unknown')} (or omitted)"

    def _stop(self, role_name: str, role: Role, tiers: list[list[Step]], args: CallArgs, lanes: dict[str, Lane], trace: list[str]) -> Stop:
        resets = {lane.name: local(lane.out_until) for lane in lanes.values() if lane.out and lane.out_until}
        resets.update({f"{lane.name}/{m}": local(b.until or 0) for lane in lanes.values() for m, b in lane.benched.items()})
        reason, say = None, role.stop
        barred = self.plan.all_barred(role)
        if barred and not tiers:
            reason, say = "loop_excluded", f"{role.stop} Nothing is left to route: " + "; ".join(barred) + "."
        consent = [x for x in role.degraded_needs_consent if not self.plan.barred(x, role)]
        ask = None
        if consent and not args.allow_degraded:
            ask = AskUser(question="No adequate model is available. Run a degraded option?", options=tuple(f"{x.model} {x.effort}" for x in consent),
                          then=f"route.py next {role_name} --allow-degraded")
        return Stop(role=role_name, say=say, reason=reason, resets=resets, ask_user=ask, trace=tuple(trace))

    def _family_state(self, fam: str, lanes: dict[str, Lane]) -> str:
        """Why no lane of `fam` is usable, in a few words: 'xai out until Sat 24 Oct 18:00', 'claude logged out', 'claude over its limit'."""
        mine = [lane for lane in lanes.values() if lane.family == fam]
        if not mine:
            return f"{fam} not set up"
        lane = mine[0]
        if lane.out:
            if lane.out_reason == "logged_out":
                return f"{fam} logged out"
            return f"{fam} out" + (f" until {local(lane.out_until)}" if lane.out_until else "")
        return f"{fam} over its limit"

    def _warning(self, step: Step, author: str | None, lanes: dict[str, Lane], picker: LanePicker) -> str | None:
        """One line for a same_family or degraded step; None for any other step."""
        if step.flag not in LATE_FLAGS:
            return None
        others = [f for f in dict.fromkeys(c.family for c in self.r.lanes.values()) if f != step.on]
        down_fams = [f for f in others if not any(picker.usable(lane, True)[0] for lane in lanes.values() if lane.family == f)]
        down = "; ".join(self._family_state(f, lanes) for f in down_fams)
        if step.flag == "same_family":
            spare = [f for f in others if f not in down_fams]
            return (f"reviewing {author or 'unknown'}'s work with {step.on}: "
                    + (f"{', '.join(spare)} is kept as the last resort" if spare else "no other family is up") + (f" ({down})" if down else ""))
        return f"degraded: {step.model} {step.effort} on {step.on} because the better options are down" + (f" ({down})" if down else "")

    def resume(self, role: str, args: CallArgs, rec: RefRecord | None, lanes: dict[str, Lane]) -> Decision:
        """Continue a vendor thread on the lane that made it; never switch lanes silently."""
        ref = args.resume
        if rec is None:
            return Error(error=f"unknown --resume ref {ref}")
        hold = self.holds.covering(rec.lane, role, self.t)
        if hold:
            return LaneOut(lane=rec.lane, outcome="held", retry_after=hold.until,
                           say=f"{rec.lane} is on hold, {hold_text(hold)}. Continue with a fresh writer on another lane; existing file changes remain in the working directory.")
        s, lane = self.r.settings, lanes.get(rec.lane)
        if lane is not None and lane.usable(s, True, rec.model)[0]:
            return Run(role=role, lane=lane.name, model=rec.model, effort=rec.effort, mode=rec.mode, cap=args.cap or rec.cap, pass_="resume",
                       rung=rec.rung, fallback=rec.fallback, fallback_reason=rec.fallback_reason, first_lane=rec.first_lane)
        why = lane.usable(s, True, rec.model)[1] if lane else "not usable by this caller"
        if lane is not None and lane.busy:       # its slots are taken: wait for one, the thread stays on its lane
            return Stop(reason="busy", lane=rec.lane, retry_after=lane.out_until, say=f"{rec.lane} has no free slot to continue {ref}: {lane.notes[0]}")
        outcome = "rate_limited"
        if lane is not None:
            outcome = lane.benched[rec.model].kind or outcome if rec.model in lane.benched else OUTCOME_OF_OUT.get(lane.out_reason or "", "rate_limited")
        return LaneOut(lane=rec.lane, outcome=outcome, retry_after=lane.back_at(s) if lane else None, say=f"{rec.lane} cannot continue {ref} now: {why}")
