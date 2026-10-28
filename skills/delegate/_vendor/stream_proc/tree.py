"""Stop a child's whole tree and prove it dead.

The child leads a session of its own, so its process group is its tree, except for descendants that
called ``setsid`` or ``setpgid`` and left it. Those are noted from the process table *before* the stop
(afterwards init adopts them and the link is gone) and signalled by pid, only while pid and start
token still match, so a reused pid is never hit.

The order: SIGTERM to the group and to each noted descendant, a grace period, SIGKILL to what
is left, then a proof loop that re-reads the table and kills again until nothing of the tree lives or
the proof time is out. Survivors are returned, never hidden. Nothing here runs ``ps`` or any tool.
Where the OS keeps no ``/proc`` only the group is signalled and the proof is "the group is empty".
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time

from stream_proc import procfs


def own_ids() -> set[int]:
    """Pids and group ids this process must never signal: itself, its group, its parent and the parent's group."""
    ids = {os.getpid(), os.getpgrp(), os.getppid()}
    with contextlib.suppress(OSError):
        ids.add(os.getpgid(os.getppid()))
    return ids


def _hit(pid: int, sig: int, *, group: bool = True) -> None:
    """Signal ``pid`` and, when ``group``, its own group; never ours, never pid or group 0 or 1."""
    if pid <= 1 or pid in own_ids():
        return
    with contextlib.suppress(OSError):
        pgid = os.getpgid(pid)
        if group and pgid > 1 and pgid not in own_ids():
            os.killpg(pgid, sig)
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _leads(p: subprocess.Popen[bytes]) -> bool:
    """True when the child leads a group of its own. An unreaped child's pid is its group id, so it cannot have been reused."""
    try:
        return os.getpgid(p.pid) == p.pid
    except OSError:
        return p.returncode is not None and p.pid not in own_ids()


def _survivors(p: subprocess.Popen[bytes], *, led: bool, noted: dict[int, str]) -> list[int]:
    """Pids of the tree still alive: the noted ones with an unchanged start token, the group's members, and all below those."""
    ours = own_ids()
    if not procfs.has_proc():
        return [p.pid] if led and not _group_empty(p.pid) else []
    rows = procfs.table()
    live = {pid for pid, start in noted.items() if procfs.alive(pid, start)}
    if p.returncode is None and procfs.alive(p.pid):
        live.add(p.pid)
    if led:
        live |= {r.pid for r in rows.values() if r.pgid == p.pid and not r.state.startswith(("Z", "X"))}
    for pid in list(live):
        live |= {r.pid for r in procfs.descendants(pid, rows) if not r.state.startswith(("Z", "X"))}
    return sorted(live - ours)


def stop_tree(p: subprocess.Popen[bytes], *, grace: float = 5.0, prove: float = 2.0) -> tuple[int, ...]:
    """Stop ``p`` and everything under it; returns the pids still alive after SIGKILL and ``prove`` seconds (normally empty).

    SIGTERM first so a child can clean up, SIGKILL after ``grace`` seconds. A child that does not lead
    its own group is signalled alone, never through a group that is ours.
    """
    led = _leads(p)
    rows = procfs.table()
    noted = {r.pid: r.start for r in procfs.descendants(p.pid, rows)}
    for pid in noted:
        _hit(pid, signal.SIGTERM)
    if led and p.pid not in own_ids():
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGTERM)
    elif p.returncode is None:
        _hit(p.pid, signal.SIGTERM, group=False)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        p.poll()
        if p.returncode is not None and not _survivors(p, led=led, noted=noted):
            return ()
        time.sleep(0.02)
    end = time.monotonic() + prove
    while True:
        if led and p.returncode is None and p.pid not in own_ids():
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL)  # unreaped: the group id is still ours to name
        left = _survivors(p, led=led, noted=noted)
        for pid in left:
            _hit(pid, signal.SIGKILL, group=pid != p.pid or led)
        p.poll()
        left = _survivors(p, led=led, noted=noted)
        if (not left and p.returncode is not None) or time.monotonic() >= end:
            break
        time.sleep(0.02)
    with contextlib.suppress(subprocess.TimeoutExpired):
        p.wait(timeout=1)
    return tuple(_survivors(p, led=led, noted=noted))
