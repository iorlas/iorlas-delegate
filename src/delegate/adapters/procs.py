"""Processes the native way (D3). A worker is spawned as the leader of a session of its own, so its process group is its tree; delegate keeps the
worker's own record (pid, pgid, start) and hands every stop to the vendored stream_proc (the only code in the repo that signals a process: tests/test_rules.py).
What lives here is what stream_proc does not know: the Worker record, the WorkerEnd report, the stdin-piped spawn and `Orphan`, a recorded worker with no Popen.

shelf lacks: (1) stop_tree's result flags term_sent / kill_sent (it returns survivor pids only; kill_sent is derived: the SIGKILL phase begins once the grace
has run out), (2) run() with a stdin pipe (the brief goes in on stdin), (3) stop by a recorded (pid, start) without a Popen (Orphan duck-types one).
POSIX only.
"""
import os
import subprocess
import time

import stream_proc
from stream_proc import alive, proc_start

from delegate.contracts import wire
from delegate.contracts.codec import record

GRACE_S = float(os.environ.get("DELEGATE_STOP_GRACE") or 5)       # SIGTERM to SIGKILL


@record
class Worker:
    """A running worker as delegate itself recorded it at spawn: the pid, its process group (a session leader's group is its pid) and its start token."""

    pid: int
    pgid: int
    start: str | None


def spawn(argv: list[str], cwd: str, env: dict[str, str]) -> tuple[subprocess.Popen[bytes], Worker]:
    """Start the worker as its own session leader with piped stdio. -> (the Popen, the worker's record)."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True)
    return p, Worker(pid=p.pid, pgid=p.pid, start=proc_start(p.pid))


class Orphan:
    """A worker delegate recorded but no longer owns (its route.py was SIGKILLed): the part of a Popen that stream_proc.stop_tree reads, by pid and start token."""

    def __init__(self, w: Worker) -> None:
        self.pid, self.start = w.pid, w.start
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and not alive(self.pid, self.start):
            self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.poll()


def stop(p: "subprocess.Popen[bytes] | Orphan", grace: float) -> wire.WorkerEnd:
    """Stop `p` and its whole tree (stream_proc.stop_tree). -> the report: SIGTERM always goes first; SIGKILL was sent once the grace ran out."""
    t0 = time.monotonic()
    rep = wire.WorkerEnd(end=wire.END_OK, stopped=True, term_sent=True)
    try:
        left = stream_proc.stop_tree(p, grace=grace)  # type: ignore[arg-type]
    except Exception:
        rep.end = wire.END_ERROR
        return rep
    rep.waited_s = round(time.monotonic() - t0, 2)
    rep.kill_sent = bool(left) or rep.waited_s >= grace
    rep.survivors = [wire.Survivor(pid=c, start=proc_start(c)) for c in left]
    if left:
        rep.end = wire.END_SURVIVED
    return rep


def kill_tree(p: subprocess.Popen, grace: float | None = None) -> wire.WorkerEnd:
    """Stop the worker `p` (a Popen) and its whole tree. -> the report; the caller keeps it."""
    return stop(p, GRACE_S if grace is None else grace)


def worker_end(stops: list[wire.WorkerEnd], outcome: str | None) -> wire.WorkerEnd:
    """The run's WorkerEnd, from the stops made during it and the run's outcome reason (None = done)."""
    survivors = [s for r in stops for s in r.survivors]
    end = wire.END_SURVIVED if survivors else wire.END_TIMEOUT if outcome == "timeout" else wire.END_ERROR if outcome else wire.END_OK
    return wire.WorkerEnd(end=end, stopped=bool(stops), term_sent=any(r.term_sent for r in stops), kill_sent=any(r.kill_sent for r in stops),
                          waited_s=round(sum(r.waited_s for r in stops), 2), survivors=survivors)


def run_capture(argv: list[str], timeout: float, **kw: object) -> subprocess.CompletedProcess[str]:
    """Run to the end with captured text output and no stdin (stream_proc.run: a session of its own; a timeout kills the whole tree).
    -> CompletedProcess; TimeoutExpired on timeout."""
    out: list[str] = []
    err: list[str] = []
    res = stream_proc.run(argv, env=kw.get("env"), cwd=kw.get("cwd"), on_stdout=out.append, on_stderr=err.append, timeout=timeout, grace=GRACE_S)  # type: ignore[arg-type]
    if res.timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, "".join(out), "".join(err))
    return subprocess.CompletedProcess(argv, -res.signal if res.signal else -1 if res.returncode is None else res.returncode, "".join(out), "".join(err))
