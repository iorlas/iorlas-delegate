"""HoldBook: the owner's holds (state.json `holds`). A hold stops one lane for some roles until a time; it always has an end.

Pure: ids and the clock come in from the caller. A hold whose lane shows little use is stale and gets ONE ask (stamped `stale_at`).
"""
from dataclasses import dataclass

from delegate.contracts.state import Hold
from delegate.domain.time.spec import local, parse_until

HOLD_STALE_BELOW = 5.0      # a lane that shows less than this % used (the weekly window, else the 5 h) has outlived its hold's reason


def hold_text(h: Hold) -> str:
    return (f"held: {h.set_by or 'owner'} " + (f"until {local(h.until)}" if h.until else "with no expiry")
            + (f" ({h.reason})" if h.reason else ""))


@dataclass(frozen=True)
class StaleHold:
    """The one ask a stale hold raises; a field of `status --json` (`asks`), never printed or pushed."""

    hold: Hold
    used_pct: float

    def as_dict(self) -> dict:
        h = self.hold
        return {"kind": "hold_stale", "id": h.id, "lane": h.lane, "raised_at": h.stale_at, "used_pct": self.used_pct,
                "say": f"hold {h.id} on {h.lane} (until {local(h.until)}{', ' + h.reason if h.reason else ''}) is still in force but the lane shows "
                       f"{self.used_pct:.0f}% used",
                "answer": f"route.py release {h.id} to end it, or leave it until it expires"}


class HoldBook:
    def __init__(self, holds: dict[str, Hold]) -> None:
        self.holds = holds

    def covering(self, lane: str, role: str | None, t: float) -> Hold | None:
        """The hold that stops `role` on `lane` at time t, else None."""
        for h in self.holds.values():
            if h.lane == lane and (not h.roles or role in h.roles) and h.until > t:
                return h
        return None

    @staticmethod
    def refusal(h: Hold, say: str | None = None) -> dict:
        """The answer fields for a refused call under a hold: the lane is out for this role until the hold ends, never `retry_after = now + 1`."""
        return {"outcome": "held", "retry_after": h.until, "say": say or f"{h.lane} is on hold, {hold_text(h)}"}

    def rows(self, t: float) -> list[dict]:
        """Every hold in force, with who set it and when it ends: for status --json and `holds`."""
        live = [h for h in self.holds.values() if h.until > t]
        return [{"id": h.id, "lane": h.lane, "roles": list(h.roles), "set_by": h.set_by, "set_at": h.set_at, "until": h.until,
                 "reason": h.reason, "stale_at": h.stale_at, "ends_in_s": int(h.until - t), "stale": bool(h.stale_at)}
                for h in sorted(live, key=lambda h: h.until or 1e18)]

    def to_stamp(self, used: dict[str, float | None], t: float) -> list[str]:
        """Ids of holds in force, not yet stamped, whose lane shows less than HOLD_STALE_BELOW % used. `used` is lane -> % (None unknown)."""
        return [k for k, h in self.holds.items()
                if h.until > t and not h.stale_at and (u := used.get(h.lane)) is not None and u < HOLD_STALE_BELOW]

    def asks(self, used: dict[str, float | None], t: float) -> list[StaleHold]:
        return [StaleHold(h, u) for h in self.holds.values()
                if h.stale_at and h.until > t and (u := used.get(h.lane)) is not None and u < HOLD_STALE_BELOW]

    def release(self, target: str) -> list[Hold]:
        """Drop the holds named by id or lane; -> the dropped ones."""
        gone = [self.holds.pop(k) for k, h in list(self.holds.items()) if target in (k, h.lane)]
        return gone


def new_hold(hold_id: str, lane: str, until: object, reason: str, roles: list[str], set_by: str, t: float) -> Hold:
    """A hold from a command line. `until` is mandatory and must be in the future: a hold with no end is refused (ValueError)."""
    end = parse_until(until, t)
    if end is None:
        raise ValueError("a hold needs --until (an epoch, an ISO time, or +2h / +3d): a hold with no end is refused")
    if end <= t:
        raise ValueError(f"--until {until} is in the past")
    return Hold(id=hold_id, lane=lane, roles=sorted(roles), set_by=set_by, set_at=t, until=end, reason=reason or "")
