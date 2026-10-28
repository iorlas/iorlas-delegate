"""The answer of `route.py status --json` (v 1), as records. `contract.json`'s `status_schema` is generated from `Status`
(dev/gen_contract.py); `app/status.py` builds the payload and passes it through these records, so a key or type the schema does not
name cannot reach a caller. `status --cheap` prints the last snapshot with `running`, `holds`, `roles`, `network` refreshed:
the same record plus `cheap`, `age_s`, `auth`."""
from dataclasses import field
from typing import Literal

from delegate.contracts.codec import ABSENT, Absent, record


@record
class LaneRow:
    """`lanes{}`: the lane's usage reading and out record."""

    family: str
    kind: str
    out: bool
    out_until: float | None
    reason: str | None = field(metadata={"doc": "why the lane is out (limit | logged_out | busy | error | credit | hung), else null"})
    five: float | None = field(metadata={"doc": "percent of the 5-hour window used, null = unknown"})
    week: float | None = field(metadata={"doc": "percent of the weekly window used, null = unknown"})
    pace: float | None
    burn: bool
    notes: list[str]


@record
class NextWorker:
    """The pick `next worker` would make now. action `run` carries pass, fallback, why; any other action carries say."""

    action: str
    lane: str | None
    model: str | None
    effort: str | None
    pass_: str | Absent = field(default=ABSENT, metadata={"name": "pass"})
    fallback: bool | Absent = ABSENT
    why: str | Absent | None = ABSENT
    say: str | Absent | None = ABSENT


@record
class Unlock:
    locked: bool
    known: bool
    at: float | None
    in_s: int | None
    text: str | None
    why: str | None


@record
class Window:
    name: str
    used_pct: float | None
    resets_at: float | None
    resets_in_s: int | None
    source: str | None
    age_s: int | None


@record
class LowTokens:
    window: str
    used_pct: float
    limit: float
    over: Literal["ceiling", "gate"]
    limit_name: str


@record
class Login:
    ok: bool | None
    why: str
    checked_at: float | None


@record
class CockpitLane:
    id: str
    label: str
    family: str
    kind: str
    model: str | None
    state: Literal["ready", "resting", "logged_out", "spend", "slow", "off", "unknown"]
    emoji: str
    reason: str
    detail: str | None
    unlock: Unlock
    windows: list[Window]
    low_tokens: list[LowTokens]
    warnings: list[str]
    meter: bool
    login: Login
    running: int


@record
class Cockpit:
    v: Literal[1]
    at: float
    legend: dict[str, str]
    next_worker: NextWorker
    lanes: list[CockpitLane]


@record
class Running:
    run_id: str
    lane: str
    role: str | None
    task: str
    started: float
    age_s: int
    pid: int
    worker_pid: int | None
    suspect: bool


@record
class NeedInput:
    what: str
    why: str
    how: str


@record
class HoldRow:
    id: str
    lane: str
    roles: list[str]
    set_by: str
    set_at: float
    until: float
    reason: str
    stale_at: float | None
    ends_in_s: int
    stale: bool


@record
class Ask:
    kind: str
    id: str
    lane: str
    raised_at: float | None
    used_pct: float
    say: str
    answer: str


@record
class Leases:
    live: int
    suspect: int


@record
class Failure:
    ts: float
    outcome: str
    say: str


@record
class HealthUsage:
    known: bool
    text: str | None


@record
class Health:
    """One row per (lane, model): state up | busy | exhausted | logged_out | held | broken."""

    lane: str
    model: str
    state: str
    until: float | None
    source: str | None
    kind: str | None
    evidence: str
    canary: bool
    rung: int
    leases: Leases
    last_failures: list[Failure]
    needs_login: bool
    usage: HealthUsage


@record
class Net:
    up: bool | None
    since: float | None
    age_s: int | None


@record
class Network:
    up: bool | None
    since: float | None
    age_s: int | None
    why: str | None


@record
class Role:
    usable: bool | None
    lane: str | None
    model: str | None
    why: str | None
    until: float | None
    hosts: list[str]
    net: Net
    due: bool | Absent = field(default=ABSENT, metadata={"doc": "`status --cheap` only: a stopped role's `until` has passed"})


@record
class CheapAuth:
    ok: bool
    why: str
    age_s: int


@record
class Status:
    V = 1

    ts: float
    lanes: dict[str, LaneRow]
    cockpit: Cockpit
    running: list[Running]
    needs_input: list[NeedInput]
    holds: list[HoldRow]
    asks: list[Ask]
    health: list[Health]
    roles: dict[str, Role]
    network: Network
    cheap: bool | Absent = ABSENT
    age_s: int | Absent | None = field(default=ABSENT, metadata={"doc": "`status --cheap` only: seconds since the snapshot"})
    auth: dict[str, CheapAuth] | Absent = field(default=ABSENT, metadata={"doc": "`status --cheap` only: the cached logins by lane"})
