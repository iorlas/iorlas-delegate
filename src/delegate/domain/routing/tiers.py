"""TierPlan: a role's steps for one call, in the order they are tried.

A tier is a list of steps tried strict, then relaxed, before the next tier. Order: a requested premium step, then every non-Claude step,
then every Claude step; within each, unflagged steps before LATE_FLAGS ones. Claude lanes come last for every role (delegate-m86).
"""
from dataclasses import replace

from delegate.contracts.routing import CallArgs, Role, Routing, Step

LATE_FLAGS = ("degraded", "same_family")
PREMIUM_FLAG = "user_requested_premium"


class TierPlan:
    def __init__(self, routing: Routing) -> None:
        self.r = routing

    def barred(self, step: Step, role: Role) -> str | None:
        """Why the loop never runs this step, else None: its model is explicit-call-only (loop false, Opus), or the role bars its family."""
        m = self.r.models.get(step.model)
        if m is not None and m.loop is False:
            return f"{step.model} is never routed in the loop (explicit user call only)"
        fam = (m.family if m else None) or step.on
        if fam in role.exclude_families:
            return f"{fam} is excluded from this role"
        return None

    def all_barred(self, role: Role) -> list[str]:
        steps = [*role.steps, *(s for v in (role.by_author or {}).values() for s in v)]
        return sorted({why for s in steps if (why := self.barred(s, role))})

    def steps_for(self, role: Role, args: CallArgs) -> list[Step] | None:
        """None when the call names an author or a premium family the role/table does not know."""
        steps = list(role.steps)
        if role.by_author is not None:
            w = args.authored_by or "unknown"
            if w not in role.by_author:
                return None
            steps = list(role.by_author[w])
        s = self.r.settings
        if not s.deepseek_free_enabled:
            steps = [x for x in steps if x.on != "deepseek"]
        if not s.zai_coding_enabled:
            steps = [x for x in steps if x.on != "zai"]
        if args.premium:
            prem = self.r.premium.get(args.premium)
            if not prem:
                return None
            steps = [prem, *steps]
        return steps

    def tiers(self, role: Role, args: CallArgs) -> list[list[Step]] | None:
        steps = self.steps_for(role, args)
        if steps is None:
            return None
        steps = [x for x in steps if not self.barred(x, role)]
        if role.order == "cost":         # cheapest configured model first inside a tier (stable: ties keep the listed order)
            steps.sort(key=lambda x: self.r.models[x.model].cost if x.model in self.r.models else 1000)
        steps = [replace(x, idx=i) for i, x in enumerate(steps)]
        first = [x for x in steps if x.flag == PREMIUM_FLAG]
        rest = [x for x in steps if x not in first]
        tiers = [first]
        for claude in (False, True):
            part = [x for x in rest if (x.on == "claude") == claude]
            tiers += [[x for x in part if x.flag not in LATE_FLAGS], [x for x in part if x.flag in LATE_FLAGS]]
        tiers = [x for x in tiers if x]
        if args.allow_degraded and role.degraded_needs_consent:
            tiers.append([replace(x, flag="degraded_with_consent", idx=len(steps) + i)
                          for i, x in enumerate(role.degraded_needs_consent) if not self.barred(x, role)])
        return tiers
