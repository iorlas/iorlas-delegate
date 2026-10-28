"""Run a child once, streaming its output, and always leave nothing of it running."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from stream_proc import procfs
from stream_proc.hold import popen_held
from stream_proc.tree import stop_tree

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Mapping, Sequence

Kind = Literal["wall", "idle", "stopped"]


@dataclass(frozen=True, slots=True)
class Result:
    """How a run ended.

    ``returncode`` is the exit code, None when a signal ended the child (then ``signal`` is its number).
    ``timed_out`` is why *we* stopped it (``wall``, ``idle``, ``stopped``), None when it ended by itself.
    ``pid``, ``pgid`` (the child's own group) and ``start`` (its start token, None without ``/proc``) name it
    for good: a pid alone can be reused. ``survivors`` are pids of its tree still alive after the stop.
    """

    returncode: int | None
    signal: int | None
    timed_out: Kind | None
    pid: int
    pgid: int
    start: str | None
    secs: float
    survivors: tuple[int, ...] = ()


def _pump(stream: object, cb: Callable[[str], None] | None, touch: Callable[[], None]) -> None:
    """Read lines until EOF (a torn last line included) so a full pipe never blocks; a failing callback never stops it."""
    for raw in stream:  # type: ignore[attr-defined]
        touch()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(raw.decode("utf-8", "replace"))


def _wait(
    p: subprocess.Popen[bytes],
    *,
    t0: float,
    last: list[float],
    timeout: float | None,
    idle: float | None,
    stop: threading.Event | None,
) -> Kind | None:
    while p.poll() is None:
        now = time.monotonic()
        if stop is not None and stop.is_set():
            return "stopped"
        if timeout is not None and now - t0 >= timeout:
            return "wall"
        if idle is not None and now - last[0] >= idle:
            return "idle"
        if stop is not None:
            stop.wait(0.02)
        else:
            time.sleep(0.02)
    return None


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    timeout: float | None = None,
    idle: float | None = None,
    stop: threading.Event | None = None,
    grace: float = 5.0,
    prove: float = 2.0,
    drain: float = 5.0,
) -> Result:
    """Run ``argv`` in a session of its own and stream each output line (newline kept) to the callbacks.

    ``timeout`` is the wall limit, ``idle`` the longest silence on both streams, ``stop`` a flag checked while
    the child runs. A limit or the flag stops the whole tree (``stop_tree``: SIGTERM, ``grace``, SIGKILL, proof
    for ``prove`` seconds). If anything unwinds the wait (KeyboardInterrupt, a SIGTERM handler that raises, even
    one landing during the spawn) the tree is stopped first and the exception goes on. ``env`` is used as given
    (None inherits); stdin is /dev/null. ``drain`` bounds the wait for the last output after the child exited.
    """
    t0 = time.monotonic()
    last = [t0]
    p, hold = popen_held(
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    readers: list[threading.Thread] = []
    kind: Kind | None = None
    survivors: tuple[int, ...] = ()
    try:
        hold.replay()
        for stream, cb in ((p.stdout, on_stdout), (p.stderr, on_stderr)):
            reader = threading.Thread(target=_pump, args=(stream, cb, lambda: last.__setitem__(0, time.monotonic())), daemon=True)
            reader.start()
            readers.append(reader)
        start = procfs.proc_start(p.pid)
        kind = _wait(p, t0=t0, last=last, timeout=timeout, idle=idle, stop=stop)
        if kind is not None:
            survivors = stop_tree(p, grace=grace, prove=prove)
    except BaseException:
        stop_tree(p, grace=grace, prove=prove)
        raise
    end = time.monotonic() + (drain if kind is None else 2.0)
    for reader in readers:
        reader.join(max(0.0, end - time.monotonic()))
    code = p.returncode
    sig = -code if code is not None and code < 0 else None
    return Result(None if sig else code, sig, kind, p.pid, p.pid, start, time.monotonic() - t0, survivors)
