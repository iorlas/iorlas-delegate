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

# The runners whose harness denies an engine-only recipe to a write step: claude -p through its PreToolUse hook, OpenCode through its bash
# permission rules. Codex has no hook under `codex exec` and `--ignore-rules` drops its execpolicy, and the Agent tool is the orchestrator's own.
ENGINE_ONLY_RUNNERS = ("claude_p", "opencode")
NO_ENGINE_ONLY = "rejected, its harness cannot deny the engine-only recipes {recipes} to a write step"
OUTCOME_OF_OUT = {"logged_out": "logged_out", "hung": "hung", "credit": "credit_exhausted"}


class Router:
    def __init__(self, routing: Routing, holds: HoldBook, prev: dict[str, Dwell], t: float) -> None:
        self.r = routing
        self.holds = holds
        self.dwell = DwellMemory(prev, routing.settings.min_dwell_minutes)
        self.t = t
        self.plan = TierPlan(routing)

    def _picker(self, lanes: dict[str, Lane], role: str | None) -> LanePicker:
        return LanePicker(self.r.settings, {n: self.holds.covering(n, role, self.t) for n in lanes}, self.t)

    def enforces_engine_only(self, lane: Lane, args: CallArgs) -> bool:
        """Can this lane's harness deny the call's engine-only recipes to a write step? An unattended agent-tool lane runs as claude -p."""
        runner = self.r.lanes[lane.name].runner
        return runner in ENGINE_ONLY_RUNNERS or (runner == "agent_tool" and args.unattended)

    def _writable(self, step: Step, args: CallArgs, lanes: dict[str, Lane], trace: list[str], skipped: list[str]) -> dict[str, Lane]:
        """Write steps require recipe enforcement unless explicitly advisory; read steps ignore the list."""
        if step.mode != "write" or not args.engine_only or args.engine_only_advisory:
            return lanes
        out = {n: lane for n, lane in lanes.items() if lane.family != step.on or self.enforces_engine_only(lane, args)}
        for n in lanes.keys() - out.keys():
            line = f"{n}: " + NO_ENGINE_ONLY.format(recipes=", ".join(args.engine_only))
            if line not in trace:
                trace.append(line)
            skipped.append(n)
        return out

    def route(self, role: str, args: CallArgs, lanes: dict[str, Lane], rec: RefRecord | None = None) -> Decision:
        """The call's decision: `--resume` continues its thread on its lane; otherwise the role's chain. An unattended caller (an engine) has
        no one to ask, and waits for a busy first choice."""
        if args.resume:
            return self.resume(role, args, rec, lanes)
        d = self.decide(role, replace(args, allow_degraded=args.allow_degraded and not args.unattended), lanes, wait_busy=args.unattended)
        return replace(d, ask_user=None) if args.unattended and isinstance(d, Stop) else d

    def decide(self, role_name: str, args: CallArgs, lanes: dict[str, Lane], wait_busy: bool = False) -> Decision:
        role = self.r.roles.get(role_name)
        if role is None:
            return Error(error=f"unknown role {role_name}; roles: {', '.join(self.r.roles)}")
        off = frozenset(c.family for n, c in self.r.lanes.items() if c.opt_in and n not in lanes)       # an opt-in lane the profile did not enable
        tiers = self.plan.tiers(role, args, off)
        if tiers is None:
            return Error(error=self._tier_error(role, args))
        s, picker = self.r.settings, self._picker(lanes, role_name)
        trace: list[str] = []
        skipped: list[str] = []
        steps = [x for tier in tiers for x in tier]
        first = next((x for x in steps if any(lane.family == x.on for lane in lanes.values())), steps[0] if steps else None)
        mine = [lane for lane in lanes.values() if first and lane.family == first.on]
        busy = [lane for lane in mine if lane.busy and lane.usable_if_free(s)]
        if busy and wait_busy and not args.only_family and not any(picker.usable(lane, True)[0] for lane in mine):
            until = min(lane.out_until or 0 for lane in busy)      # the first choice is only waiting for a slot: a minute beats a round on the next rung
            return Stop(reason="busy", lane=busy[0].name, retry_after=until, trace=(),
                        say=f"{busy[0].name} (the role's first choice) has no free slot: {busy[0].notes[0]}")
        why_not_first = "busy" if busy else "out" if any(lane.out for lane in mine) else "ceiling"
        first_lane = next((lane.name for lane in mine if lane.kind == "subscription"), mine[0].name if mine else None)
        for tier in tiers:
            for relaxed in (False, True):
                for step in tier:
                    lane = picker.pick(step.on, self._writable(step, args, lanes, trace, skipped), relaxed, trace, step.model)
                    if lane is None:
                        continue
                    fb = step is not first
                    return self._advisory(Run(role=role_name, lane=lane.name, model=step.model, effort=step.effort, mode=step.mode, cap=step.cap, flag=step.flag,
                               pass_="relaxed" if relaxed else "strict", rung=step.idx, fallback=fb, fallback_reason=why_not_first if fb else None,
                               first_lane=first_lane if fb else None, trace=tuple(trace),
                               warning=self._warning(step, args.authored_by, lanes, picker)), lane, args)
        if args.only_family:
            busy = [lane for step in steps for lane in self._writable(step, args, lanes, trace, skipped).values()
                    if lane.family == step.on and (lane.busy or lane.out_reason == "busy")
                    and not (lane.out and lane.out_reason != "busy")
                    and picker.usable(replace(lane, out=False), True, step.model)[0]]
            if busy:
                lane = min(busy, key=lambda lane: lane.out_until or 0)
                return Stop(reason="busy", lane=lane.name, retry_after=lane.out_until, trace=tuple(trace),
                            say=f"--only-family {', '.join(args.only_family)}: {self._family_state(lane.family, {lane.name: lane})}")
        stop = self._stop(role_name, role, tiers, args, lanes, trace)
        return self._typed_stop(stop, args, lanes, skipped)

    def _typed_stop(self, stop: Stop, args: CallArgs, lanes: dict[str, Lane], skipped: list[str]) -> Stop:
        """Explain enforcement rejections before the generic family-unready stop."""
        if stop.reason is not None:
            return stop
        if skipped:
            names = ", ".join(sorted(set(skipped)))
            return replace(stop, reason="engine_only_unenforced", ask_user=None,
                           say=f"{stop.say} --engine-only {', '.join(args.engine_only)} cannot be denied to a write step on {names}: the lane is not used for it. Use --engine-only-advisory to proceed with a warning; the brief must say so.")
        if args.only_family:
            return self._only_family_stop(stop, args, lanes)
        return stop

    def _advisory(self, run: Run, lane: Lane, args: CallArgs) -> Run:
        if run.mode != "write" or not args.engine_only or not args.engine_only_advisory or self.enforces_engine_only(lane, args):
            return run
        warning = f"{lane.name} cannot deny --engine-only {', '.join(args.engine_only)}: advisory only, the brief must say so"
        return replace(run, flag="engine_only_advisory", warning="; ".join(x for x in (run.warning, warning) if x))

    def _only_family_stop(self, stop: Stop, args: CallArgs, lanes: dict[str, Lane]) -> Stop:
        down = "; ".join(self._family_state(f, lanes, stop.role) for f in args.only_family)
        return replace(stop, reason="only_family_unready", ask_user=None,
                       say=f"{stop.say} --only-family {', '.join(args.only_family)}: no step of that family is ready ({down}); no other family is used.")

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
        consent = [x for x in role.degraded_needs_consent if not self.plan.barred(x, role) and self.plan.in_family(x, args)]
        ask = None
        if consent and not args.allow_degraded:
            ask = AskUser(question="No adequate model is available. Run a degraded option?", options=tuple(f"{x.model} {x.effort}" for x in consent),
                          then=f"route.py next {role_name} --allow-degraded")
        return Stop(role=role_name, say=say, reason=reason, resets=resets, ask_user=ask, trace=tuple(trace))

    def _family_state(self, fam: str, lanes: dict[str, Lane], role: str | None = None) -> str:
        """Why no lane of `fam` is usable, without mistaking full slots for usage limits."""
        mine = [lane for lane in lanes.values() if lane.family == fam]
        if not mine:
            return f"{fam} not set up"
        lane = next((lane for lane in mine if (lane.busy or lane.out_reason == "busy")
                     and not (lane.out and lane.out_reason != "busy")
                     and replace(lane, out=False).usable(self.r.settings, True,
                         hold=self.holds.covering(lane.name, role, self.t))[0]), mine[0])
        if lane.out and lane.out_reason != "busy":
            return self._family_out_state(fam, lane)
        free = replace(lane, out=False) if lane.out_reason == "busy" else lane
        usable, why = free.usable(self.r.settings, True)
        if why.startswith(("weekly", "5h")):
            return f"{fam} over its limit"
        hold = self.holds.covering(lane.name, role, self.t)
        if hold:
            return f"{fam} {hold_text(hold)}"
        if usable and (lane.busy or lane.out_reason == "busy"):
            note = next((note for note in lane.notes if note.startswith("busy:")), "busy")
            return f"{fam} {note}"
        return f"{fam} not ready"

    @staticmethod
    def _family_out_state(fam: str, lane: Lane) -> str:
        if lane.out_reason == "logged_out":
            return f"{fam} logged out"
        return f"{fam} out" + (f" until {local(lane.out_until)}" if lane.out_until else "")

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
        if lane is not None and rec.mode == "write" and args.engine_only and not args.engine_only_advisory and not self.enforces_engine_only(lane, args):
            return Stop(role=role, reason="engine_only_unenforced", lane=lane.name, trace=(f"{lane.name}: " + NO_ENGINE_ONLY.format(recipes=", ".join(args.engine_only)),),
                        say=f"{ref} runs on {lane.name}, whose harness cannot deny --engine-only {', '.join(args.engine_only)} to a write step. Start a fresh writer on another lane or use --engine-only-advisory; the brief must say so.")
        if lane is not None and lane.usable(s, True, rec.model)[0]:
            return self._advisory(Run(role=role, lane=lane.name, model=rec.model, effort=rec.effort, mode=rec.mode, cap=args.cap or rec.cap, pass_="resume",
                       rung=rec.rung, fallback=rec.fallback, fallback_reason=rec.fallback_reason, first_lane=rec.first_lane), lane, args)
        why = lane.usable(s, True, rec.model)[1] if lane else "not usable by this caller"
        if lane is not None and lane.busy:       # its slots are taken: wait for one, the thread stays on its lane
            return Stop(reason="busy", lane=rec.lane, retry_after=lane.out_until, say=f"{rec.lane} has no free slot to continue {ref}: {lane.notes[0]}")
        outcome = "rate_limited"
        if lane is not None:
            outcome = lane.benched[rec.model].kind or outcome if rec.model in lane.benched else OUTCOME_OF_OUT.get(lane.out_reason or "", "rate_limited")
        return LaneOut(lane=rec.lane, outcome=outcome, retry_after=lane.back_at(s) if lane else None, say=f"{rec.lane} cannot continue {ref} now: {why}")
