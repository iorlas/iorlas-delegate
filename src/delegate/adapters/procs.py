"""Processes the native way (D3). A worker is spawned as the leader of a session of its own, so its process group is its tree; delegate keeps the
worker's own record (pid, pgid, start) and signals the group. Nothing here runs `ps`, `lsof` or any other tool: the start token and the process
table come from /proc where the OS has one (elsewhere the group signal alone carries the stop). POSIX only.

`stop_tree` signals every process group and pid it knows below the worker (SIGTERM, a grace, SIGKILL, re-collecting): the Codex CLI is a node shim
whose native child may sit in a group of its own and ignore SIGTERM. It returns a wire.WorkerEnd that names the survivors (pid + start token).
"""
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable

from delegate.contracts import wire
from delegate.contracts.codec import record

PROC = "/proc"
GRACE_S = float(os.environ.get("DELEGATE_STOP_GRACE") or 5)       # SIGTERM to SIGKILL


@record
class Worker:
    """A running worker as delegate itself recorded it at spawn: the pid, its process group (a session leader's group is its pid) and its start token."""

    pid: int
    pgid: int
    start: str | None


def _stat(pid: int) -> tuple[str, int, int, str] | None:
    """/proc/<pid>/stat as (state, ppid, pgid, starttime ticks), or None. The command name may hold spaces and parens: split after the last ')'."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        with open(os.path.join(PROC, str(int(pid)), "stat"), encoding="utf-8", errors="replace") as f:
            rest = f.read().rpartition(")")[2].split()
        return rest[0], int(rest[1]), int(rest[2]), rest[19]
    return None


def has_proc() -> bool:
    return os.path.isfile(os.path.join(PROC, "self", "stat"))


def start_token(pid: int | None) -> str | None:
    """A token that, with the pid, names one process (not a reused pid): None when it is gone or a zombie, or the OS keeps no /proc."""
    s = _stat(pid) if pid else None
    return None if not s or s[0].startswith("Z") else s[3]


def alive(pid: int | None) -> bool:
    """True while the pid is a live, non-zombie process."""
    if not pid:
        return False
    if has_proc():
        return start_token(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def table() -> dict[int, tuple[int, int]]:
    """{pid: (ppid, pgid)} of every live process; {} where the OS keeps no /proc."""
    out: dict[int, tuple[int, int]] = {}
    with contextlib.suppress(OSError):
        for name in os.listdir(PROC):
            s = _stat(int(name)) if name.isdigit() else None
            if s and not s[0].startswith("Z"):
                out[int(name)] = (s[1], s[2])
    return out


def descendants(pid: int, tab: dict[int, tuple[int, int]] | None = None) -> list[int]:
    """Every live process below `pid`, by parent links (whatever session or group each sits in)."""
    kids: dict[int, list[int]] = {}
    for c, (pp, _) in (table() if tab is None else tab).items():
        kids.setdefault(pp, []).append(c)
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def spawn(argv: list[str], cwd: str, env: dict[str, str]) -> tuple[subprocess.Popen[bytes], Worker]:
    """Start the worker as its own session leader with piped stdio. -> (the Popen, the worker's record)."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, start_new_session=True)
    return p, Worker(pid=p.pid, pgid=p.pid, start=start_token(p.pid))


def _signal_all(groups: set[int], pids: set[int], sig: int) -> None:
    for g in groups:
        with contextlib.suppress(OSError):
            os.killpg(g, sig)
    for c in pids:
        with contextlib.suppress(OSError):
            os.kill(c, sig)


def stop_tree(pid: int, grace: float = 0, done: Callable[[], bool] | None = None, pgid: int | None = None,
              is_alive: Callable[[int], bool] | None = None) -> wire.WorkerEnd:
    """-> WorkerEnd. With `grace`, SIGTERM first and wait up to `grace` s for `done()` (default: nothing known is alive); then SIGKILL."""
    rep = wire.WorkerEnd(end=wire.END_OK, stopped=True)
    is_alive = is_alive or alive
    me, mygroup, t0 = os.getpid(), os.getpgrp(), time.time()
    known, groups = {pid}, {pgid or pid}

    def collect() -> None:
        tab = table()
        known.update([pid] + descendants(pid, tab))      # a dead parent orphans its children: pids are kept from before the first signal
        groups.update(tab[c][1] for c in known if c in tab)
        groups.discard(mygroup)
        known.discard(me)

    def left() -> list[int]:
        return [c for c in known if is_alive(c)]
    collect()
    if grace:
        _signal_all(groups, known, signal.SIGTERM)
        rep.term_sent = True
        end = time.time() + grace
        while time.time() < end and not (done() if done else not left()):
            time.sleep(0.1)
    for _ in range(5):
        collect()
        if not left():
            break
        _signal_all(groups, known, signal.SIGKILL)
        rep.kill_sent = True
        time.sleep(0.05)
    rep.waited_s = round(time.time() - t0, 2)
    rep.survivors = [wire.Survivor(pid=c, start=start_token(c)) for c in left()]
    if rep.survivors:
        rep.end = wire.END_SURVIVED
    return rep


def kill_tree(p: subprocess.Popen, grace: float | None = None) -> wire.WorkerEnd:
    """Stop the worker `p` (a Popen) and its whole tree. -> the report; the caller keeps it."""
    rep = wire.WorkerEnd(end=wire.END_ERROR, stopped=True)
    with contextlib.suppress(Exception):
        rep = stop_tree(p.pid, GRACE_S if grace is None else grace, is_alive=lambda c: p.poll() is None if c == p.pid else alive(c))
    with contextlib.suppress(Exception):
        p.kill()
    return rep


def worker_end(stops: list[wire.WorkerEnd], outcome: str | None) -> wire.WorkerEnd:
    """The run's WorkerEnd, from the stops made during it and the run's outcome reason (None = done)."""
    survivors = [s for r in stops for s in r.survivors]
    end = wire.END_SURVIVED if survivors else wire.END_TIMEOUT if outcome == "timeout" else wire.END_ERROR if outcome else wire.END_OK
    return wire.WorkerEnd(end=end, stopped=bool(stops), term_sent=any(r.term_sent for r in stops), kill_sent=any(r.kill_sent for r in stops),
                          waited_s=round(sum(r.waited_s for r in stops), 2), survivors=survivors)


def run_capture(argv: list[str], timeout: float, **kw: object) -> subprocess.CompletedProcess[str]:
    """subprocess.run with captured text output and no stdin, in a session of its own; a timeout (or any exception) kills the whole tree, not just the
    child. -> CompletedProcess; TimeoutExpired on timeout."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, **kw)  # type: ignore[call-overload]
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        kill_tree(p)            # the whole tree: a node shim's native child may sit in a session of its own
        with contextlib.suppress(Exception):
            p.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)
