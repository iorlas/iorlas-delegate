"""The Harness Protocol: everything delegate knows about one agent CLI lives in one class that implements it (D8).

A harness builds the launch (argv, env, stdin), turns each line of the CLI's output into HarnessEvents ONCE, keeps the facts those lines
established (`evidence`), finds the transcript and probes the login. It knows nothing of lanes, state, holds or the router.
"""
from dataclasses import field
from typing import Protocol

from delegate.contracts.codec import mutable, record
from delegate.contracts.events import HarnessEvent


@record
class LaunchInputs:
    """What the router decided and the run needs, for one launch."""

    exe: str
    cwd: str
    model: str
    effort: str
    sandbox: str
    prompt: str
    out_file: str
    env: dict[str, str]
    cli: list[str]                  # the router's argv for a fresh run (`codex exec -C ... -s ... -m ...`), exe first
    access: list[str]               # delegate's fixed sandbox and network flags
    isolation: list[str]            # skills turned off by path
    schema: str | None = None
    resume: str | None = None


@record
class LaunchSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    stdin: str


@mutable
class Evidence:
    """The facts a stream established, for the supervisor and the classifier."""

    ref: str | None = None
    last: str = ""                  # the last agent message
    tokens: dict[str, int | None] | None = None
    cumulative: dict[str, int | None] | None = None      # the thread's running total on a resume
    turns: int = 0
    work: bool = False              # a command, edit, tool call or message finished
    error: str = ""                 # the harness's own failure text
    transient: str = ""             # a retry in progress ("Reconnecting... 2/5", "waiting for network")
    net_at: float | None = None     # monotonic time the harness began waiting for the network; None = the stream moves
    declined: list[str] = field(default_factory=list)      # commands a reviewer refused


class Harness(Protocol):
    kind: str
    evidence: Evidence

    def launch_spec(self, i: LaunchInputs) -> LaunchSpec: ...

    def parse_line(self, line: str) -> list[HarnessEvent]: ...

    def vendor_error(self, rc: int | None) -> str: ...

    def transcript(self, ref: str | None) -> str | None: ...

    def login_probe(self, timeout: float) -> tuple[bool | None, str]: ...
