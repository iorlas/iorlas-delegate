"""LanePicker: which lane of one family serves a model right now (usable, then subscription vs credit)."""
from delegate.contracts.routing import Settings
from delegate.contracts.state import Hold
from delegate.domain.lanes.lane import Lane
from delegate.domain.routing.dwell import DwellMemory


class LanePicker:
    def __init__(self, settings: Settings, holds: dict[str, Hold | None], dwell: DwellMemory, t: float) -> None:
        self.s = settings
        self.holds = holds          # lane name -> the hold covering the role being decided
        self.dwell = dwell
        self.t = t

    def usable(self, lane: Lane, relaxed: bool, model: str | None = None) -> tuple[bool, str]:
        return lane.usable(self.s, relaxed, model, self.holds.get(lane.name))

    def pick(self, family: str, lanes: dict[str, Lane], relaxed: bool, trace: list[str], model: str | None = None) -> Lane | None:
        """The lane of `family` to use now for `model`, or None. Appends the reasoning to `trace`."""
        ok = []
        for lane in (x for x in lanes.values() if x.family == family):
            good, why = self.usable(lane, relaxed, model)
            trace.append(f"{lane.name}: {'usable' if good else 'rejected, ' + why}")
            if good:
                ok.append(lane)
        if not ok:
            return None
        sub = next((x for x in ok if x.kind == "subscription"), None)
        credit = next((x for x in ok if x.kind == "credit"), None)
        if not (sub and credit):
            return ok[0]
        return self._sub_or_credit(family, sub, credit, relaxed, trace)

    def _sub_or_credit(self, family: str, sub: Lane, credit: Lane, relaxed: bool, trace: list[str]) -> Lane:
        s = self.s
        if not relaxed and not sub.usage_known:
            trace.append(f"{sub.name}: usage unknown, so {credit.name} first")
            return credit
        if sub.burn:        # use-it-or-lose-it
            trace.append(f"{sub.name}: BURN")
            return sub
        c = s.credit
        if credit.reset_in_days is not None and credit.reset_in_days <= c.use_before_reset_days and credit.spend < c.under_cap_fraction * (credit.cap or 0):
            trace.append(f"{credit.name}: resets in {credit.reset_in_days:.1f}d with the allowance unspent")
            return credit
        keep = self.dwell.sticky(family, (sub.name, credit.name), self.t)      # strained / recovered, with a minimum dwell so the choice does not flap
        if keep:
            trace.append(f"dwell: keep {keep} for {s.min_dwell_minutes} min")
            return sub if keep == sub.name else credit
        if (sub.five is not None and sub.five >= s.strain.five_hour) or (sub.pace is not None and sub.pace <= s.strain.weekly_pace):
            trace.append(f"{sub.name}: strained")
            return credit
        if (sub.five is None or sub.five < s.recover.five_hour) and (sub.pace is None or sub.pace > s.recover.weekly_pace):
            return sub
        last = self.dwell.last(family)
        return sub if (not last or last == sub.name) else credit
