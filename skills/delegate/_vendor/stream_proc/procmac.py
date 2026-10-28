"""The process table on macOS, from ``libproc`` through ``ctypes``: the same rows as ``procfs``.

No ``ps``, no shell, no external tool: ``proc_listpids`` and ``proc_listchildpids`` name the processes,
``proc_pidinfo`` with ``PROC_PIDTBSDINFO`` gives ppid, pgid, status, command and the START TIME
(``pbi_start_tvsec``/``pbi_start_tvusec``), which is the start token (a reused pid cannot share it), and
with ``PROC_PIDTASKINFO`` the CPU time (user + system, mach ticks converted by ``mach_timebase_info``).

The structs are written as ``ctypes.Structure`` and are importable anywhere (tests pin their size and
field offsets on every OS); only :func:`_lib` touches the dylib, lazily, and only on macOS. Where it
cannot load, :func:`has_table` is False and everything here answers "unavailable", never raises.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import sys

from stream_proc.procfs import Proc, signal0

PROC_ALL_PIDS = 1
PROC_PIDTBSDINFO = 3
PROC_PIDTASKINFO = 4
SZOMB = 5  # pbi_status of a zombie (<sys/proc.h>)
MAXCOMLEN = 16


class ProcBsdInfo(ctypes.Structure):
    """``struct proc_bsdinfo`` of ``<sys/proc_info.h>``: 136 bytes."""

    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


class ProcTaskInfo(ctypes.Structure):
    """``struct proc_taskinfo`` of ``<sys/proc_info.h>``: 96 bytes."""

    _fields_ = (
        ("pti_virtual_size", ctypes.c_uint64),
        ("pti_resident_size", ctypes.c_uint64),
        ("pti_total_user", ctypes.c_uint64),
        ("pti_total_system", ctypes.c_uint64),
        ("pti_threads_user", ctypes.c_uint64),
        ("pti_threads_system", ctypes.c_uint64),
        ("pti_policy", ctypes.c_int32),
        ("pti_faults", ctypes.c_int32),
        ("pti_pageins", ctypes.c_int32),
        ("pti_cow_faults", ctypes.c_int32),
        ("pti_messages_sent", ctypes.c_int32),
        ("pti_messages_received", ctypes.c_int32),
        ("pti_syscalls_mach", ctypes.c_int32),
        ("pti_syscalls_unix", ctypes.c_int32),
        ("pti_csw", ctypes.c_int32),
        ("pti_threadnum", ctypes.c_int32),
        ("pti_numrunning", ctypes.c_int32),
        ("pti_priority", ctypes.c_int32),
    )


class TimebaseInfo(ctypes.Structure):
    """``mach_timebase_info_data_t``: ``ns = ticks * mul / div``."""

    _fields_ = (("mul", ctypes.c_uint32), ("div", ctypes.c_uint32))


@functools.cache
def _lib() -> ctypes.CDLL | None:
    """libproc (re-exported by libSystem), or None off macOS or when it will not load. Touched only here."""
    if sys.platform != "darwin":
        return None
    for name in ("libproc.dylib", "/usr/lib/libSystem.B.dylib"):
        with contextlib.suppress(OSError, AttributeError):
            lib = ctypes.CDLL(name, use_errno=True)
            lib.proc_listpids.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
            lib.proc_listpids.restype = ctypes.c_int
            lib.proc_listchildpids.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
            lib.proc_listchildpids.restype = ctypes.c_int
            lib.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
            lib.proc_pidinfo.restype = ctypes.c_int
            lib.mach_timebase_info.argtypes = (ctypes.POINTER(TimebaseInfo),)
            lib.mach_timebase_info.restype = ctypes.c_int
            return lib
    return None


@functools.cache
def _timebase() -> tuple[int, int]:
    """(mul, div) converting mach ticks to nanoseconds; (1, 1) where unknown."""
    lib = _lib()
    info = TimebaseInfo()
    if lib is not None and lib.mach_timebase_info(ctypes.byref(info)) == 0 and info.mul and info.div:
        return info.mul, info.div
    return 1, 1


def has_table() -> bool:
    return _lib() is not None


def _bsdinfo(pid: int) -> ProcBsdInfo | None:
    lib = _lib()
    if lib is None or pid <= 0:
        return None
    info = ProcBsdInfo()
    got = lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return info if got == ctypes.sizeof(info) and info.pbi_pid == pid else None


def _cpu_s(pid: int) -> float:
    """User + system CPU seconds so far; 0.0 where macOS refuses the task info (another user's process)."""
    lib = _lib()
    if lib is None:
        return 0.0
    task = ProcTaskInfo()
    if lib.proc_pidinfo(pid, PROC_PIDTASKINFO, 0, ctypes.byref(task), ctypes.sizeof(task)) != ctypes.sizeof(task):
        return 0.0
    mul, div = _timebase()
    return (task.pti_total_user + task.pti_total_system) * mul / div / 1e9


def _token(info: ProcBsdInfo) -> str:
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


def read(pid: int) -> Proc | None:
    """One process as a ``Proc``, or None when it is gone or unreadable. ``sid`` is 0 (libproc does not give it)."""
    info = _bsdinfo(pid)
    if info is None:
        return None
    state = "Z" if info.pbi_status == SZOMB else "R"
    cmd = (info.pbi_comm or info.pbi_name).decode("utf-8", errors="replace")
    return Proc(pid, info.pbi_ppid, info.pbi_pgid, 0, _token(info), state, _cpu_s(pid), cmd)


def all_pids() -> list[int]:
    lib = _lib()
    if lib is None:
        return []
    for _ in range(4):  # the table can grow between the size query and the read
        size = lib.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
        if size <= 0:
            return []
        buf = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 64))()
        got = lib.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
        if 0 < got < ctypes.sizeof(buf):
            return [p for p in buf[: got // ctypes.sizeof(ctypes.c_int)] if p > 0]
    return []


def child_pids(pid: int) -> list[int]:
    """Direct children of ``pid`` by ``proc_listchildpids``.

    Its return value is a count on some releases and a byte count on others, so the buffer is zeroed and read to the
    first 0; a full buffer is retried bigger.
    """
    lib = _lib()
    if lib is None or pid <= 0:
        return []
    cap = 64
    while True:
        buf = (ctypes.c_int * cap)()
        got = lib.proc_listchildpids(pid, buf, ctypes.sizeof(buf))
        if got < 0:
            return []
        kids = [p for p in buf if p > 0]
        if len(kids) < cap or cap >= 65536:
            return kids
        cap *= 4


def table() -> dict[int, Proc]:
    """Every process now, by pid. Empty where libproc is unavailable."""
    rows: dict[int, Proc] = {}
    for pid in all_pids():
        if (row := read(pid)) is not None:
            rows[pid] = row
    return rows


def descendants(root: int) -> list[Proc]:
    """Every process under ``root`` by ``proc_listchildpids``, children before grandchildren."""
    found: list[Proc] = []
    seen = {root}
    todo = [root]
    while todo:
        for pid in child_pids(todo.pop(0)):
            if pid not in seen:
                seen.add(pid)
                todo.append(pid)
                if (row := read(pid)) is not None:
                    found.append(row)
    return found


def proc_start(pid: int | None) -> str | None:
    """The start token (``<sec>.<usec>``) of a live (non-zombie) process; None when gone, a zombie or libproc is unavailable."""
    info = _bsdinfo(pid) if pid else None
    return None if info is None or info.pbi_status == SZOMB else _token(info)


def alive(pid: int | None, start: str | None = None) -> bool:
    """True while ``pid`` is a live process, and, given ``start``, still the one that began at that token.

    If libproc cannot load, signal 0 answers and the token cannot be checked.
    """
    if not pid:
        return False
    if has_table():
        token = proc_start(pid)
        return token is not None and (not start or token == start)
    return signal0(pid)
