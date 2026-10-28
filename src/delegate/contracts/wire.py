"""The wire between a caller and `route.py run`: RunRequest in (argv + env), one RunResult JSON object out (contract 3).

`contract.json`'s answer_schema is generated from RunResult (dev/gen_contract.py). A key the wire leaves out when nobody set it is ABSENT;
null is a value. The live JSONL a caller may tail is contracts/live.py.
"""
import sys
from dataclasses import field
from typing import Any, Literal

from delegate.contracts.codec import ABSENT, Absent, mutable, record

CONTRACT = 3
END_OK, END_ERROR, END_TIMEOUT, END_SURVIVED = "ok", "error", "timeout", "worker_survived"
Scope = Literal["bead", "caller", "machine", "lane", "model"]
Permanence = Literal["transient", "needs_owner"]
RetryKind = Literal["none", "predicted", "poll"]
TranscriptStatus = Literal["ok", "ref_unresolvable", "died_before_session_id", "not_launched", "unsupported"]
TranscriptKind = Literal["codex", "claude", "opencode_export"]
DenialKind = Literal["policy", "sandbox_limit", "secret"]
AskKind = Literal["decision"]


@record
class Survivor:
    pid: int
    start: str | None = None      # lstart / starttime token: with the pid it names one process


@mutable
class WorkerEnd:
    """How the worker's life ended. pid, pgid and start name the process the delegate launched (it runs in its own session: pgid == pid)."""

    end: Literal["ok", "error", "timeout", "worker_survived"] = END_OK
    stopped: bool = False            # the delegate stopped the worker (timeout, idle, answer given, SIGTERM to delegate), it did not exit alone
    term_sent: bool = False
    kill_sent: bool = False
    waited_s: float = 0.0
    survivors: list[Survivor] = field(default_factory=list)
    pid: int | Absent = ABSENT
    pgid: int | Absent = ABSENT
    start: str | None | Absent = ABSENT


@record
class RunRequest:
    role: str
    brief_file: str
    cwd: str
    schema_file: str | None = None
    resume: str | None = None
    authored_by: str | None = None
    timeout_s: int | None = None     # wall limit for the call; delegate stops the worker's process group at it
    idle_s: int | None = None        # no output this long: the worker is hung (delegate's own watchdog; unset = the CLI's default)
    unattended: bool = True
    parent: str = ""
    task: str = ""
    title: str = ""
    call_id: str | None = None
    live_file: str | None = None     # DELEGATE_LIVE_FILE
    engine_only: list[str] = field(default_factory=list)     # recipes only the engine runs: a write step's guard blocks `just RECIPE`; --engine-only once per item

    def to_argv(self, route: str) -> list[str]:
        out = [sys.executable, route, "run", self.role, "--brief", self.brief_file, "--cwd", self.cwd]
        opt = [("--schema", self.schema_file), ("--resume", self.resume), ("--authored-by", self.authored_by), ("--timeout", self.timeout_s),
               ("--idle", self.idle_s), ("--parent", self.parent), ("--task", self.task), ("--title", self.title[:120]), ("--call-id", self.call_id)]
        for flag, v in opt:
            if v not in (None, ""):
                out += [flag, str(v)]
        for recipe in self.engine_only:
            out += ["--engine-only", recipe]
        return out + (["--unattended"] if self.unattended else [])

    def env(self) -> dict[str, str]:
        return {"DELEGATE_LIVE_FILE": self.live_file} if self.live_file else {}


@record
class Tokens:
    """Input includes cached input, per call, never cumulative."""

    in_: int | None = field(metadata={"name": "in"})
    cached: int | None
    out: int | None


@record
class UsageTokens:
    """usage.tokens: Tokens plus the vendor-neutral split (cache_write is null where the vendor does not report it)."""

    in_: int | None = field(metadata={"name": "in"})
    cached: int | None
    out: int | None
    in_fresh: int | None
    cache_read: int | None
    cache_write: int | None
    cache_write_by_ttl: dict[str, int] | Absent = ABSENT


@record
class Denied:
    kind: DenialKind
    tool: str | None
    path: str | None
    input: dict[str, Any]


@record
class TranscriptInfo:
    path: str
    kind: TranscriptKind
    from_: int = field(metadata={"name": "from"})
    to: int
    bytes: int


@record
class Retry:
    kind: RetryKind
    at: float | None


@record
class StopFacts:
    reason: str
    scope: Scope
    permanence: Permanence
    retry: Retry
    ref: str | None
    fix: str | Absent = ABSENT
    resumable: bool | Absent = ABSENT
    partial: bool | Absent = ABSENT


@record
class Usage:
    tokens: UsageTokens | Tokens | None
    cost: float
    secs: float
    turns: int | None
    context_k: float | None
    answer_bytes: int
    ref: str | None
    last_activity_at: str | None
    warm_until: str | None
    ttl_s: int | None
    ttl_source: Literal["vendor_default", "observed", "unknown"]
    cost_usd: float | None
    cost_source: Literal["harness", "unknown", "free"]
    accounting: Literal["exact", "estimated", "unknown"]


@record
class Context:
    instructions: list[str]
    env_keys: list[str]


@record
class AskUser:
    question: str
    options: list[str]
    then: str


@record
class RunResult:
    """The answer of `route.py run`. stop is null exactly when the call is done or is the agent-tool handoff (agent_model set)."""

    contract: Literal[3]
    data: Any
    text: str
    ref: str | None
    lane: str | None
    model: str | None
    effort: str | None
    family: str | None
    flag: str | None
    cost: float
    tokens: Tokens | None
    denied: list[Denied]
    secs: float
    run_id: str | None
    transcript: str | None
    say: str
    git_violation: list[str]
    call_id: str | None
    transcript_info: TranscriptInfo | None
    transcript_status: TranscriptStatus | None
    stop: StopFacts | None
    usage: Usage
    rung: int | None | Absent = ABSENT
    fallback: bool | None | Absent = ABSENT
    fallback_reason: str | None | Absent = ABSENT
    first_lane: str | None | Absent = ABSENT
    warnings: list[str] | Absent = ABSENT
    notice: str | Absent = ABSENT
    warning: str | Absent = ABSENT
    needs_input: list[str] | Absent = ABSENT
    ask_user: AskUser | Absent = ABSENT
    ask_kind: AskKind | Absent = field(default=ABSENT, metadata={"doc": "what the caller is asked, when ask_user is set"})
    agent_model: str | Absent = field(default=ABSENT, metadata={
        "doc": "set only on the agent-tool handoff (stop is null): the model the orchestrator's Agent tool should run"})
    brief_prefix: str | Absent = ABSENT
    cwd: str | Absent = ABSENT
    context: Context | Absent = ABSENT
    exit_code: int | None | Absent = field(default=ABSENT, metadata={"doc": "the worker's exit status (negative = killed by that signal)"})
    first_error: str | Absent = field(default=ABSENT, metadata={"doc": "the first error the harness or the process reported, cut to 240 characters"})
    worker: WorkerEnd | Absent = field(default=ABSENT, metadata={"doc": "how the worker's life ended; absent when no worker was launched"})

    @property
    def reason(self) -> str | None:
        return self.stop.reason if self.stop else None
