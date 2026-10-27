"""The typed contract between a caller (task-loop vendors this file) and `route.py run`: stdlib dataclasses only, no import from delegate.

RunRequest -> argv + env for `route.py run`; the answer's JSON -> RunResult. The live JSONL a caller may tail (DELEGATE_LIVE_FILE) is defined by
lanes/live.py. A worker's life ends in one of four ways, `WorkerEnd.end`: ok, error, timeout, worker_survived (pids of what outlived SIGTERM and SIGKILL).
"""
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional

END_OK, END_ERROR, END_TIMEOUT, END_SURVIVED = "ok", "error", "timeout", "worker_survived"
ENDS = (END_OK, END_ERROR, END_TIMEOUT, END_SURVIVED)
CONTRACT = 3


@dataclass
class Survivor:
    pid: int
    start: Optional[str] = None      # lstart / starttime token: with the pid it names one process


@dataclass
class WorkerEnd:
    end: str = END_OK
    stopped: bool = False            # the delegate stopped the worker (timeout, idle, answer given, SIGTERM to delegate), it did not exit alone
    term_sent: bool = False
    kill_sent: bool = False
    waited_s: float = 0.0
    survivors: List[Survivor] = field(default_factory=list)

    def wire(self):
        return asdict(self)

    @classmethod
    def parse(cls, d):
        d = dict(d or {})
        d["survivors"] = [Survivor(**s) for s in d.get("survivors") or []]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class RunRequest:
    role: str
    brief_file: str
    cwd: str
    schema_file: Optional[str] = None
    resume: Optional[str] = None
    authored_by: Optional[str] = None
    timeout_s: Optional[int] = None  # wall limit for the call; delegate stops the worker's process group at it
    idle_s: Optional[int] = None     # no output this long: the worker is hung (delegate's own watchdog; unset = the CLI's default)
    unattended: bool = True
    parent: str = ""
    task: str = ""
    title: str = ""
    call_id: Optional[str] = None
    live_file: Optional[str] = None  # DELEGATE_LIVE_FILE

    def argv(self, route):
        a = [sys.executable, route, "run", self.role, "--brief", self.brief_file, "--cwd", self.cwd]
        opt = [("--schema", self.schema_file), ("--resume", self.resume), ("--authored-by", self.authored_by), ("--timeout", self.timeout_s),
               ("--idle", self.idle_s), ("--parent", self.parent), ("--task", self.task), ("--title", self.title[:120]), ("--call-id", self.call_id)]
        for flag, v in opt:
            if v not in (None, ""):
                a += [flag, str(v)]
        return a + (["--unattended"] if self.unattended else [])

    def env(self):
        return {"DELEGATE_LIVE_FILE": self.live_file} if self.live_file else {}


@dataclass
class RunResult:
    contract: int
    stop: Optional[dict]             # null = done; else {reason, scope, permanence, retry, ...} (contract.json)
    worker: WorkerEnd
    text: str = ""
    data: Optional[dict] = None
    ref: Optional[str] = None
    run_id: Optional[str] = None
    secs: float = 0.0
    cost: float = 0.0

    @property
    def reason(self):
        return (self.stop or {}).get("reason")

    @classmethod
    def parse(cls, answer):
        """The answer object `route.py run` printed -> RunResult. A missing `worker` (a refusal before any launch) is a clean ok."""
        if answer.get("contract") != CONTRACT:
            raise ValueError("route.py answers contract %r, this caller needs %d" % (answer.get("contract"), CONTRACT))
        return cls(answer["contract"], answer.get("stop"), WorkerEnd.parse(answer.get("worker")), answer.get("text") or "", answer.get("data"),
                   answer.get("ref"), answer.get("run_id"), float(answer.get("secs") or 0), float(answer.get("cost") or 0))
