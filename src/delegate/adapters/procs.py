"""Processes the native way: a worker runs in a session of its own, so its process group is its whole tree; delegate keeps the worker's own
record (pid, pgid, start) and signals the group. Nothing here runs `ps`; the start token comes from /proc where there is one."""
import contextlib
import os
import signal
import subprocess

PROC = "/proc"


def run_capture(argv: list[str], timeout: float, **kw: object) -> subprocess.CompletedProcess:
    """subprocess.run with captured text output, no stdin, in a session of its own; a timeout (or any exception) kills the group, not just the child.
    -> CompletedProcess; TimeoutExpired on timeout."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, **kw)  # type: ignore[call-overload]
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            p.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)
