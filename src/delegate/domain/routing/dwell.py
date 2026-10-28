"""DwellMemory: the lane last chosen per family (state.json `prev`), so a choice between two lanes of one family does not flap."""
from delegate.contracts.state import Dwell


class DwellMemory:
    def __init__(self, prev: dict[str, Dwell], minutes: float) -> None:
        self.prev = prev
        self.minutes = minutes

    def last(self, family: str) -> str | None:
        d = self.prev.get(family)
        return d.lane if d else None

    def sticky(self, family: str, names: tuple[str, ...], t: float) -> str | None:
        """The lane to keep because it was chosen less than `minutes` ago and is one of `names`."""
        d = self.prev.get(family)
        return d.lane if d and d.lane in names and t - d.since < self.minutes * 60 else None

    def note(self, family: str, lane: str, t: float) -> Dwell | None:
        """The record to store when the choice changed, else None."""
        d = self.prev.get(family)
        return None if d and d.lane == lane else Dwell(lane=lane, since=t)
