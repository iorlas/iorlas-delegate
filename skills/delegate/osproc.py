"""The one place that knows how this OS lists processes (delegate-own). POSIX: /proc on Linux, `ps` on macOS; Windows answers "nothing found".

Everything else (reaper, route) asks four questions: who is below pid P (`descendants`), is pid P the same process I saw before (`proc_start`),
is it alive and not a zombie (`alive`), which group is it in (`pgid`). No other module runs ps, lsof or tasklist.
"""
import contextlib
import os
import subprocess

POSIX = os.name != "nt"
PROC = "/proc"


def _linux():
    return os.path.isfile(os.path.join(PROC, "self", "stat"))


def _stat(pid):
    """/proc/<pid>/stat as (state, ppid, pgid, starttime ticks), or None. The command name may hold spaces and parens: split after the last ')'."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        with open(os.path.join(PROC, str(int(pid)), "stat"), encoding="utf-8", errors="replace") as f:
            rest = f.read().rpartition(")")[2].split()
        return rest[0], int(rest[1]), int(rest[2]), rest[19]
    return None


def _ps(*args):
    with contextlib.suppress(Exception):
        return subprocess.run(["ps", *args], capture_output=True, text=True, timeout=5).stdout
    return ""


def table():
    """{pid: (ppid, pgid)} of every live process; {} where the OS cannot say."""
    if not POSIX:
        return {}
    out = {}
    if _linux():
        with contextlib.suppress(OSError):
            for name in os.listdir(PROC):
                s = _stat(name) if name.isdigit() else None
                if s and not s[0].startswith("Z"):
                    out[int(name)] = (s[1], s[2])
        return out
    for ln in _ps("-A", "-o", "pid=,ppid=,pgid=").splitlines():
        with contextlib.suppress(ValueError):
            p, pp, pg = ln.split()
            out[int(p)] = (int(pp), int(pg))
    return out


def descendants(pid, tab=None):
    """Every live process below `pid`, by parent links (whatever session or group each sits in)."""
    kids = {}
    for c, (pp, _) in (table() if tab is None else tab).items():
        kids.setdefault(pp, []).append(c)
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def proc_start(pid):
    """A token that, with the pid, names one process (not a reused pid): None when it is gone or a zombie, or the OS cannot say."""
    if not POSIX or not pid:
        return None
    if _linux():
        s = _stat(pid)
        return None if not s or s[0].startswith("Z") else s[3]
    with contextlib.suppress(Exception):
        stat, _, start = _ps("-o", "stat=,lstart=", "-p", str(int(pid))).strip().partition(" ")
        return None if not stat or stat.startswith("Z") else start.strip() or None
    return None


def alive(pid):
    """True while the pid is a live, non-zombie process."""
    return proc_start(pid) is not None


def pgid(pid):
    with contextlib.suppress(OSError):
        return os.getpgid(pid)
    return None
