"""Lane: what the router knows about one lane right now: its spec, the usage reading, the out record, the breaker's view of its models.

A reader fills a Lane in as it builds the fleet; from then on nothing mutates it: a hold is passed to the question that needs it, and a
lane that is only waiting for a slot is a COPY (`with_busy`).
"""
from dataclasses import dataclass, field, replace

from delegate.contracts.routing import Settings
from delegate.contracts.state import Hold
from delegate.domain.health.ledger import PairView
from delegate.domain.holds.book import hold_text
from timefmt import fmt_span

from delegate.domain.time.spec import local


@dataclass
class Lane:
    name: str
    family: str
    kind: str                            # subscription | credit
    shared: bool = False                 # shared with the orchestrator: keeps its reserve
    out: bool = False
    out_until: float | None = None
    out_reason: str | None = None        # limit | logged_out | busy | error | credit | hung
    read_at: float | None = None         # when the usage reading was taken (Codex only)
    busy: bool = False                   # out only because its slots are taken: it frees up by itself in minutes
    five: float | None = None            # percent of the 5-hour window used, None = unknown
    week: float | None = None            # percent of the weekly window used
    week_reset: float | None = None      # epoch
    five_reset: float | None = None
    pace: float | None = None            # weekly pace in points: elapsed share minus used share
    burn: bool = False                   # weekly window about to reset with allowance left
    reset_in_days: float | None = None   # credit lanes
    spend: float = 0.0
    cap: float | None = None
    notes: list[str] = field(default_factory=list)
    windows: list[dict] = field(default_factory=list)       # every window the vendor exposes, for the cockpit
    spend_blocked: bool = False          # the vendor says a spend or credit limit stopped it (not a usage window)
    warn: list[str] = field(default_factory=list)           # a reading the owner should double-check (shown as `!` lines)
    benched: dict[str, PairView] = field(default_factory=dict)      # model -> a pair the breaker benched
    canary: dict[str, bool] = field(default_factory=dict)           # model -> the pair's bench ended: the next launch is the one canary
    hview: dict[str, PairView] = field(default_factory=dict)        # model -> the breaker's view of every known pair

    @property
    def usage_known(self) -> bool:
        return self.five is not None or self.week is not None

    def limits(self, s: Settings, relaxed: bool) -> tuple[float, float, str]:
        """-> (weekly %, five-hour %, name): where the router stops using the lane. The strict pass uses the gate (a shared lane keeps the
        orchestrator's reserve), the relaxed pass the ceiling."""
        c = s.relaxed_ceiling
        if self.shared:
            r = s.orchestrator_reserve
            wk, fv = (c.orchestrator_weekly, c.orchestrator_five_hour) if relaxed else (r.weekly, r.five_hour)
            return wk, fv, "hard ceiling" if relaxed else "orchestrator reserve"
        g = s.worker_lane_gate
        wk, fv = (c.weekly, c.five_hour) if relaxed else (g.weekly, g.five_hour)
        return wk, fv, "hard ceiling" if relaxed else "lane gate"

    def usable(self, s: Settings, relaxed: bool, model: str | None = None, hold: Hold | None = None) -> tuple[bool, str]:
        """-> (bool, reason). With `model`, a pair the breaker benched is refused too; `hold` is the owner's hold covering this role."""
        if hold:
            return False, hold_text(hold)
        if model and model in self.benched:
            b = self.benched[model]
            return False, f"{model} benched ({b.kind}, {b.source}) until {local(b.until or 0)}"
        if self.out:
            return False, f"out until {local(self.out_until)}" if self.out_until else "out"
        wk, fv, why = self.limits(s, relaxed)
        if self.week is not None and self.week >= wk:
            return False, f"weekly {self.week:.0f}% >= {wk} ({why})"
        if self.five is not None and self.five >= fv:
            return False, f"5h {self.five:.0f}% >= {fv} ({why})"
        return True, "ok"

    def back_at(self, s: Settings, hold: Hold | None = None) -> float | None:
        """When an unusable lane is expected back: its hold's end, else its out-until, else the reset of the window it is over."""
        ok, why = self.usable(s, True, hold=hold)
        if ok:
            return None
        if hold:
            return hold.until
        if self.out:
            return self.out_until
        return self.week_reset if why.startswith("weekly") else self.five_reset if why.startswith("5h") else None

    def slots(self, s: Settings) -> int | None:
        """How many runs may be in flight: max_parallel (None = unlimited), one when the lane is strained."""
        if s.max_parallel is None:
            return None
        if self.five is not None and self.five >= s.strain.five_hour:
            return 1
        return int(s.max_parallel)

    def with_busy(self, until: float, running: int, cap: int) -> "Lane":
        """A copy that is out only because its slots are taken: it is skipped like a refused lane."""
        return replace(self, out=True, out_until=until, busy=True, out_reason="busy", notes=[f"busy: {running} running, {cap} allowed", *self.notes])

    def usable_if_free(self, s: Settings) -> bool:
        """Would the lane be usable (relaxed) if its slots were free?"""
        return replace(self, out=False).usable(s, True)[0]

    def brief(self, t: float) -> str:
        bits = []
        if self.five is not None:
            bits.append(f"5h {self.five:.0f}%" + (f" (resets {fmt_span(self.five_reset - t)})" if self.five_reset and self.five_reset > t else ""))
        if self.week is not None:
            reset = f" (resets {fmt_span(self.week_reset - t)}, {local(self.week_reset)})" if self.week_reset and self.week_reset > t else ""
            bits.append(f"week {self.week:.0f}%" + reset + (f" pace {self.pace:+.0f}" if self.pace is not None else ""))
        if self.kind == "credit":
            bits.append(f"cycle ${self.spend:.2f} of ${self.cap or 0:.0f} cap by this tool"
                        + (f", resets in {self.reset_in_days:.1f}d" if self.reset_in_days is not None else ""))
        if self.out:
            bits.append("OUT" + (f" until {local(self.out_until)}" if self.out_until else ""))
        elif self.burn:
            bits.append("BURN (about to be lost: spend it)")
        if not self.usage_known and self.kind == "subscription":
            bits.append("usage unknown")
        return "; ".join(bits + self.notes)


def window(used: float | None, resets_at: float | None, span_min: float, t: float) -> tuple[float | None, float | None, float | None]:
    """-> (used_percent, minutes_to_reset, pace). A window whose reset passed counts as 0% used."""
    if used is None:
        return None, None, None
    if resets_at is not None and resets_at <= t:
        return 0.0, 0.0, None
    if resets_at is None:
        return float(used), None, None
    mins = (resets_at - t) / 60.0
    elapsed = max(0.0, min(100.0, 100.0 * (1 - mins / span_min)))
    return float(used), mins, elapsed - float(used)


def apply_burn(lane: Lane, s: Settings, week_mins: float | None) -> None:
    """The weekly window is about to reset with allowance left: spend it."""
    b = s.burn
    lane.burn = (lane.week is not None and week_mins is not None and 0 < week_mins <= b.minutes
                 and 100 - lane.week >= b.min_left_percent and (lane.five is None or lane.five < 60))
