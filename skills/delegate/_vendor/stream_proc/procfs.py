"""The process table from ``/proc``, and liveness that survives pid reuse.

A pid alone names no process: once its holder is reaped the number is handed to a stranger.
The pair (pid, start token) does: the token is the process's start time in clock ticks since
boot (``/proc/<pid>/stat`` field 22), which a reused pid cannot share. No ``ps``, no external
tool. Where the OS keeps no ``/proc`` this module has no table: ``proc_start`` says ``None`` and
``alive`` falls back to signal 0 on the pid. macOS has its own module (``stream_proc.procmac``); callers
use the selector ``stream_proc.proctable``, which gives the same API on both.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and "SC_CLK_TCK" in os.sysconf_names else 100


@dataclass(frozen=True, slots=True)
class Proc:
    """One row of the process table. ``state`` is the kernel's letter (``Z`` zombie)."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    start: str
    state: str
    cpu_s: float = 0.0
    cmd: str = ""


def has_proc() -> bool:
    return (PROC / "self" / "stat").is_file()


def read(pid: int) -> Proc | None:
    """``/proc/<pid>/stat`` as a ``Proc``, or None when it is gone or unreadable.

    The command name may hold spaces and parentheses, so the fields are split after the last ``)``.
    """
    with contextlib.suppress(OSError, ValueError, IndexError):
        head, _, tail = (PROC / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace").rpartition(")")
        rest = tail.split()
        cpu = (int(rest[11]) + int(rest[12])) / CLK_TCK
        return Proc(int(pid), int(rest[1]), int(rest[2]), int(rest[3]), rest[19], rest[0], cpu, head.partition("(")[2])
    return None


def table() -> dict[int, Proc]:
    """Every process now, by pid. Empty where there is no ``/proc``."""
    rows: dict[int, Proc] = {}
    with contextlib.suppress(OSError):
        for entry in PROC.iterdir():
            if entry.name.isdigit() and (row := read(int(entry.name))) is not None:
                rows[row.pid] = row
    return rows


def proc_start(pid: int | None) -> str | None:
    """The start token of a live (non-zombie) process; None when it is gone, a zombie, or the OS keeps no ``/proc``."""
    row = read(pid) if pid else None
    return None if row is None or row.state.startswith(("Z", "X")) else row.start


def alive(pid: int | None, start: str | None = None) -> bool:
    """True while ``pid`` is a live process, and, given ``start``, still the one that began at that token.

    Without ``/proc`` the token cannot be checked and a bare signal 0 answers (a reused pid then counts as alive).
    """
    if not pid:
        return False
    if has_proc():
        token = proc_start(pid)
        return token is not None and (not start or token == start)
    return signal0(pid)


def signal0(pid: int) -> bool:
    """True when signal 0 reaches ``pid`` (a pid we may not signal still exists); the tokenless fallback."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists
        return True
    return True


def descendants(root: int, rows: dict[int, Proc]) -> list[Proc]:
    """Every process under ``root`` in ``rows``, children before grandchildren."""
    kids: dict[int, list[Proc]] = {}
    for row in rows.values():
        kids.setdefault(row.ppid, []).append(row)
    found: list[Proc] = []
    seen = {root}
    todo = [root]
    while todo:
        for kid in kids.get(todo.pop(0), []):
            if kid.pid not in seen:
                seen.add(kid.pid)
                found.append(kid)
                todo.append(kid.pid)
    return found
