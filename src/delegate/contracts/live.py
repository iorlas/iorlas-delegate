"""The live file (DELEGATE_LIVE_FILE): what a worker is doing right now, one JSON object per line.

The first line is a WorkerStarted (no `k`): where the worker process is. Every other line is a LiveEvent: `t` (epoch) and `k` (kind), then the
kind's fields. Kinds that open and close something come in _start/_end pairs, matched by `id`. `prov` marks a row derived before the harness
confirmed it. A value the harness did not give is left out of the line, not written as null (`exit` of cmd_end is the exception: null = unknown).
"""
from dataclasses import field
from typing import ClassVar, Literal

from delegate.contracts.codec import ABSENT, Absent, record, table_of


@record
class WorkerStarted:
    V: ClassVar[int] = 1
    pid: int
    pgid: int
    run_id: str


@record
class LiveRec:
    TAG: ClassVar[str] = "k"
    t: float


@record
class LiveStart(LiveRec):
    KIND: ClassVar[str] = "start"
    harness: str
    pid: int
    lane: str | Absent = ABSENT
    model: str | Absent = ABSENT
    effort: str | Absent = ABSENT
    role: str | Absent = ABSENT


@record
class LiveCmdStart(LiveRec):
    KIND: ClassVar[str] = "cmd_start"
    id: str
    text: str
    cwd: str | Absent = ABSENT
    prov: bool | Absent = ABSENT


@record
class LiveCmdEnd(LiveRec):
    KIND: ClassVar[str] = "cmd_end"
    id: str
    exit: int | None
    secs: float
    out: str | Absent = ABSENT


@record
class LiveToolStart(LiveRec):
    KIND: ClassVar[str] = "tool_start"
    id: str
    text: str
    prov: bool | Absent = ABSENT


@record
class LiveToolEnd(LiveRec):
    KIND: ClassVar[str] = "tool_end"
    id: str
    secs: float
    out: str | Absent = ABSENT


@record
class LiveMsg(LiveRec):
    KIND: ClassVar[str] = "msg"
    text: str


@record
class LiveThink(LiveRec):
    KIND: ClassVar[str] = "think"
    text: str


@record
class LiveTokens(LiveRec):
    KIND: ClassVar[str] = "tokens"
    in_: int | Absent = field(default=ABSENT, metadata={"name": "in"})
    cached: int | Absent = ABSENT
    out: int | Absent = ABSENT


@record
class LiveStall(LiveRec):
    KIND: ClassVar[str] = "stall"
    secs: int


@record
class LiveRetry(LiveRec):
    """The harness is retrying by itself (Codex "Reconnecting... 2/5", HTTP 429): n of `of` attempts."""

    KIND: ClassVar[str] = "retry"
    n: int | Absent = ABSENT
    of: int | Absent = ABSENT
    why: str | Absent = ABSENT


@record
class LiveEnd(LiveRec):
    KIND: ClassVar[str] = "end"
    status: Literal["done", "timeout", "killed", "error"]
    secs: float
    cost_usd: float | Absent = ABSENT


@record
class LiveRaw(LiveRec):
    """An event this reader does not map: the type name only, never the payload."""

    KIND: ClassVar[str] = "raw"
    type: str


LiveEvent = (LiveStart | LiveCmdStart | LiveCmdEnd | LiveToolStart | LiveToolEnd | LiveMsg | LiveThink | LiveTokens | LiveStall | LiveRetry
             | LiveEnd | LiveRaw)
LIVE_EVENTS = table_of(LiveStart, LiveCmdStart, LiveCmdEnd, LiveToolStart, LiveToolEnd, LiveMsg, LiveThink, LiveTokens, LiveStall, LiveRetry,
                       LiveEnd, LiveRaw)
LIVE_FILE_MAX = 20 * 1024 * 1024      # a live file past this stops growing; the call goes on
