"""LanePicker: which lane of one family serves a model right now (the first usable one)."""
from delegate.contracts.routing import Settings
from delegate.contracts.state import Hold
from delegate.domain.lanes.lane import Lane


class LanePicker:
    def __init__(self, settings: Settings, holds: dict[str, Hold | None], t: float) -> None:
        self.s = settings
        self.holds = holds          # lane name -> the hold covering the role being decided
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
        return ok[0]
