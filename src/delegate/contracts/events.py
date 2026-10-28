"""HarnessEvent: what a harness's parser says happened, one record per fact. In memory only (never written to disk): each harness's
`parse_line` turns one line of its output into zero or more of these, once, and the supervisor, the live sink and the classifier all read the
same records."""
from typing import Any

from delegate.contracts.codec import record


@record
class Started:
    ref: str


@record
class Cmd:
    id: str | None
    text: str
    cwd: str | None = None
    prov: bool = False              # provisional: derived from a log line before the harness reported the call (closed by the next CmdEnd of its id)


@record
class CmdEnd:
    id: str | None
    text: str
    exit: int | None
    out: str | None = None
    secs: float | None = None       # when the harness measured it; else the sink times it


@record
class Tool:
    id: str | None
    name: str
    arg: str = ""
    prov: bool = False


@record
class ToolEnd:
    id: str | None
    name: str
    arg: str = ""
    out: str | None = None
    secs: float | None = None


@record
class Msg:
    text: str
    final: bool = True              # this message ends the turn (false: the worker goes on, e.g. text before a tool call)


@record
class Think:
    text: str


@record
class Tokens:
    in_: int | None
    cached: int | None
    out: int | None
    cache_write: int | None = None


@record
class TurnDone:
    usage: Tokens | None


@record
class Stall:
    secs: float


@record
class HarnessError:
    text: str
    http_status: int | None = None
    kind: str = "error"             # "error" (a failure) or "transient" (the harness is retrying by itself)


@record
class Reconnecting:
    n: int
    of: int
    text: str = ""


@record
class WaitingForNetwork:
    text: str = ""


@record
class RateLimit:
    until: float | None = None


@record
class Denied:
    tool: str
    input: dict[str, Any]
    kind: str = "policy"            # "policy" (a reviewer or classifier refused: the owner decides) or "sandbox_limit" (the sandbox blocked a system resource: noise)


@record
class Unknown:
    type: str


HarnessEvent = (Started | Cmd | CmdEnd | Tool | ToolEnd | Msg | Think | Tokens | TurnDone | Stall | HarnessError | Reconnecting
                | WaitingForNetwork | RateLimit | Denied | Unknown)
