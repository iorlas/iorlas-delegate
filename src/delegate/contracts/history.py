"""`history.jsonl` (one `HistoryRow` per line, `v` 1, tagged by `kind`), `ledger.jsonl` and `health-checkpoint.json`.

A row is a fact, appended once and never edited. The health fold (domain/health) reads `run`, `canary` and `release` rows; the rest are for
calibration and the dashboard. A line of another shape is skipped, never migrated.
"""
from dataclasses import field
from typing import Any

from delegate.contracts.codec import mutable, record, table_of
from delegate.contracts.routing import BreakerPolicy

V = 1


@record
class RunRow:
    """A worker run that ended (done or not). `done` and `outcome` are the ladder's verdict (outcome None = done)."""

    KIND = "run"
    V = V
    ts: float
    run_id: str
    role: str
    lane: str
    model: str
    effort: str
    done: bool
    task: str = ""
    call_id: str | None = None
    ref: str | None = None
    outcome: str | None = None
    fingerprint: str | None = None
    say: str | None = None
    until: float | None = None
    warnings: list[str] | None = None
    stream_tail: Any = None
    denials: int = 0
    secs: float | None = None
    cost: float = 0
    tokens: dict[str, Any] | None = None
    cumulative: dict[str, Any] | None = None
    five_before: float | None = None
    week_before: float | None = None
    five_after: float | None = None
    week_after: float | None = None
    overlap: list[str] = field(default_factory=list)
    slots: int | None = None
    transcript: str | None = None


@record
class RefusalRow:
    """An answer given without launching a worker; kept so refusals are countable. Never a health fact."""

    KIND = "refusal"
    V = V
    ts: float
    outcome: str
    role: str
    call_id: str | None = None
    fingerprint: str | None = None
    say: str | None = None
    until: float | None = None
    task: str = ""
    lane: str | None = None
    model: str | None = None
    resume: bool = False


@record
class CanaryRow:
    """The one probe after a bench ended answered: a `done` for the pair."""

    KIND = "canary"
    V = V
    ts: float
    run_id: str
    lane: str
    model: str


@record
class GapRow:
    KIND = "gap"
    V = V
    ts: float
    lane: str
    model: str
    gap_s: float
    finished: bool


@record
class ReleaseRow:
    """`route.py report --event ok`: the owner says the lane is fine, the fold forgets its limits."""

    KIND = "release"
    V = V
    ts: float
    lane: str


@record
class ReapRow:
    KIND = "reap"
    V = V
    ts: float
    run_id: str
    lane: str
    reaped: bool
    role: str | None = None
    task: str = ""
    worker_pid: int | None = None


HistoryRow = RunRow | RefusalRow | CanaryRow | GapRow | ReleaseRow | ReapRow
ROWS = table_of(RunRow, RefusalRow, CanaryRow, GapRow, ReleaseRow, ReapRow)


@record
class LedgerRow:
    V = V
    ts: float
    model: str
    cost: float


@mutable
class Streak:
    ts: float
    outcome: str
    fp: str
    task: str | None


@mutable
class Failure:
    ts: float
    outcome: str
    say: str


@mutable
class PairFact:
    streak: list[Streak] = field(default_factory=list)
    rung: int = 0
    until: float = 0.0
    kind: str | None = None
    fp: str | None = None
    evidence: str = ""
    source: str | None = None
    fails: list[Failure] = field(default_factory=list)
    last_done: float = 0.0


@mutable
class LaneFact:
    kind: str
    until: float
    source: str
    evidence: str
    ts: float


@mutable
class HealthState:
    V = V
    ts: float = 0.0
    pairs: dict[str, PairFact] = field(default_factory=dict)       # "LANE|model"
    lanes: dict[str, LaneFact] = field(default_factory=dict)


@record
class HealthCheckpoint:
    """A cache of the fold up to `state.ts`, valid only for the breaker policy it was built under."""

    V = V
    policy: BreakerPolicy
    state: HealthState
