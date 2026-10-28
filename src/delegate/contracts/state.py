"""`state.json`: everything the router remembers between calls, one typed document (`v` 1).

Sections have one owner each (see domain/): `out` the OutBoard, `holds` the HoldBook, `leases` the LeaseBook, `prev` the DwellMemory,
`auth` the one login cache (the old auth-cache.json is merged in), `codex_limits` / `claude_usage` the usage readers' caches. Every write
goes through `StateStore.transaction()` under one lock. A file of another shape reads as an empty document: nothing is migrated.
"""
from dataclasses import field
from typing import Any

from delegate.contracts.codec import mutable

V = 1


@mutable
class OutState:
    """A lane marked out. source: vendor (the vendor stated the end) | guess | manual; `set` is the wall clock of the write (running workers read it)."""

    until: float
    why: str = ""
    at: float = 0.0
    set: float = 0.0
    reason: str = "limit"
    source: str = "guess"
    zone: str | None = None
    guess_n: int = 0


@mutable
class Hold:
    id: str
    lane: str
    until: float
    set_at: float
    roles: list[str] = field(default_factory=list)       # empty: every role
    set_by: str = "owner"
    reason: str = ""
    stale_at: float | None = None


@mutable
class Lease:
    """A running `run`: renewed while its route.py lives; an expired one is dropped unless its process still runs."""

    lane: str
    pid: int
    pid_start: str | None
    start: float
    expires: float
    role: str | None = None
    task: str = ""
    silence_s: float | None = None
    canary: str | None = None
    worker_pid: int | None = None
    worker_start: str | None = None
    last_event: float | None = None
    ref: str | None = None


@mutable
class RefRecord:
    """What a vendor session id was started with, for `--resume`."""

    lane: str
    model: str
    effort: str
    mode: str
    cap: float | None = None
    rung: int | None = None
    fallback: bool | None = None
    fallback_reason: str | None = None
    first_lane: str | None = None


@mutable
class Dwell:
    lane: str
    since: float


@mutable
class AuthEntry:
    at: float
    ok: bool
    why: str = ""


@mutable
class UsageCache:
    at: float
    data: Any = None


@mutable
class StateDoc:
    V = V
    out: dict[str, OutState] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    refs: dict[str, RefRecord] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    prev: dict[str, Dwell] = field(default_factory=dict)
    auth: dict[str, AuthEntry] = field(default_factory=dict)
    warned_at: dict[str, float] = field(default_factory=dict)
    codex_limits: UsageCache | None = None
    claude_usage: UsageCache | None = None
    codex_login_pid: int | None = None
