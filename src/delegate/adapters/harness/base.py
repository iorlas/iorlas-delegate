"""The Harness Protocol: everything delegate knows about one agent CLI lives in one class that implements it (D8).

A harness builds the launch (argv, env, stdin), turns each line of the CLI's output into HarnessEvents ONCE, keeps the facts those lines
established (`evidence`), finds the transcript and probes the login. It knows nothing of lanes, state, holds or the router.
"""
from dataclasses import field
from typing import Protocol

from delegate.contracts.codec import mutable, record
from delegate.contracts import events as ev
from delegate.contracts.events import HarnessEvent


@record
class LaunchInputs:
    """What the router decided and the run needs, for one launch."""

    exe: str
    cwd: str
    model: str
    effort: str
    prompt: str                     # codex: rules and brief on stdin; claude and opencode: the brief (opencode: rules first)
    env: dict[str, str]
    schema: str | None = None       # path of the JSON Schema file
    resume: str | None = None
    sandbox: str = ""               # codex
    out_file: str = ""              # codex
    cli: list[str] = field(default_factory=list)            # codex: the router's argv for a fresh run (`codex exec -C ... -s ... -m ...`), exe first
    access: list[str] = field(default_factory=list)         # codex: delegate's fixed sandbox and network flags
    isolation: list[str] = field(default_factory=list)      # codex: skills turned off by path
    session: str = ""               # claude: the session id we fix up front (--session-id)
    system: str = ""                # claude: the appended system prompt
    settings: str = ""              # claude: --settings JSON (the PreToolUse guard)
    write: bool = False             # claude, opencode: may the worker edit
    budget: float | None = None     # claude: --max-budget-usd on the credit lane


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
    denials: list[ev.Denied] = field(default_factory=list)       # what the harness or its reviewer refused, in order
    final: dict | None = None       # claude: the first `result` event
    context_k: float | None = None  # the last request's whole input, in thousands of tokens
    steps: list[dict] = field(default_factory=list)         # raw usage the harness reported per request or step (claude: usage, opencode: {tokens, cost})


@mutable
class Summary:
    """What a finished stream says, for the one place that settles a run: the answer text, the result fields the harness owns, and the inputs
    of the outcome ladder."""

    text: str = ""
    rc: int | None = None
    res: dict = field(default_factory=dict)         # ref, tokens, turns, context_k, started, partial, acct, ...
    vendor: str = ""                # the CLI's own error text (never the model's answer)
    stderr: str = ""                # the stderr the ladder may read
    work: bool = False              # the worker did something before it failed
    check: bool = True              # classify even when rc == 0
    stands: bool = False            # an answer we stopped after stays an answer
    warnings: list[str] = field(default_factory=list)         # kept when the run is otherwise clean
    cost: float = 0.0


class Harness(Protocol):
    kind: str
    evidence: Evidence

    def launch_spec(self, i: LaunchInputs) -> LaunchSpec: ...

    def parse_line(self, line: str) -> list[HarnessEvent]: ...

    def parse_log_line(self, line: str) -> list[HarnessEvent]: ...

    def summary(self, rc: int | None, out: str, err: str, stop: str | None, timed_out: bool) -> Summary: ...

    def vendor_error(self, rc: int | None) -> str: ...

    def transcript(self, ref: str | None) -> str | None: ...

    def login_probe(self, timeout: float) -> tuple[bool | None, str]: ...
