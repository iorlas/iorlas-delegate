"""One process owns a path: an exclusive ``flock``, held until the process lets go.

- **Exclusive.** A second process asking for the lock is refused at once
  (:class:`LockHeldError`), or waits up to ``timeout`` seconds, calling ``on_wait`` as it does.
- **Released by the kernel.** A ``flock`` dies with its holder, ``kill -9`` included.
- **A structured holder record.** The lock file holds one JSON record (pid, created timestamp, free-form ``label``
  and ``meta``), readable by
  :func:`holder` without taking the lock. Informational only; no host name.
- **Never broken.** A lock the kernel says is held is never taken over. If the recorded pid is
  gone, a forked child kept the descriptor; the error says so and the caller decides.
- **Re-entrant within a process** by default: two handles on one descriptor, released at the
  last :meth:`ProcessLock.release`. ``reentrant=False`` makes a second acquire of the same
  path in this process wait or fail like any other holder. Not ``lockf``: a POSIX record lock
  is dropped when *any* descriptor on the file closes.
- **Not inherited.** The descriptor is non-inheritable (PEP 446), so a child does not keep the lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_READ_BYTES = 65536
_POLL = 0.05


@dataclass(frozen=True)
class Holder:
    """The record a holder wrote into its lock file."""

    pid: int
    created: float = 0.0
    label: str | None = None
    meta: dict[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True)
class Waiting:
    """What ``on_wait`` is told on each poll: how long, and who holds the lock (when readable)."""

    path: Path
    elapsed: float
    holder: Holder | None


class LockHeldError(Exception):
    """Another holder owns :attr:`path` (:attr:`pid` when its record names one) past the timeout."""

    def __init__(self, path: Path, pid: int | None, holder: Holder | None = None) -> None:
        self.path = path
        self.pid = pid
        self.holder = holder
        if pid is not None and holder is not None and not _alive(pid):
            super().__init__(f"{path} is locked: held by an inherited descriptor; recorded holder pid {pid} is gone")
            return
        who = f"pid {pid}" if pid is not None else "an unknown process"
        super().__init__(f"{path} is locked by another process ({who})")


@dataclass
class _Held:
    fd: int
    reentrant: bool
    count: int = 0


_REGISTRY: dict[Path, _Held] = {}
_REGISTRY_LOCK = threading.Lock()


def held() -> frozenset[Path]:
    """The resolved lock-file paths this process holds now."""
    with _REGISTRY_LOCK:
        return frozenset(_REGISTRY)


def holder(path: Path) -> Holder | None:
    """The holder record in ``path``, read without taking the lock; None when absent or unreadable.

    It says who wrote the record last, not that they still hold the lock.
    """
    for _ in range(3):  # a writer may be mid-update
        try:
            raw = path.read_bytes()[:_READ_BYTES]
        except OSError:
            return None
        parsed = _parse(raw)
        if parsed is not None:
            return parsed
        time.sleep(0.005)
    return None


@dataclass
class ProcessLock:
    """One handle on this process's lock of :attr:`path`. :meth:`release` is idempotent."""

    path: Path
    _released: bool = field(default=False, repr=False)

    @classmethod
    def acquire(
        cls,
        path: Path,
        *,
        timeout: float | None = 0.0,
        on_wait: Callable[[Waiting], None] | None = None,
        label: str | None = None,
        meta: Mapping[str, object] | None = None,
        reentrant: bool = True,
    ) -> ProcessLock:
        """Lock ``path`` for this process.

        ``timeout`` is seconds to wait (0: refuse at once, None: wait forever); on expiry raise
        :class:`LockHeldError`. ``on_wait`` is called on every poll while waiting.
        """
        key = path.resolve()
        record = {"label": label, "meta": dict(meta or {})}
        json.dumps(record)  # refuse an unserializable record before taking anything
        began = time.monotonic()
        while True:
            seen = _try(key, record, reentrant=reentrant)
            if seen is None:
                return cls(path=key)
            elapsed = time.monotonic() - began
            if timeout is not None and elapsed >= timeout:
                raise LockHeldError(key, seen.holder.pid if seen.holder else None, seen.holder)
            if on_wait is not None:
                on_wait(Waiting(key, elapsed, seen.holder))
            time.sleep(_POLL if timeout is None else max(0.0, min(_POLL, timeout - elapsed)))

    def release(self) -> None:
        """Give up this handle; the lock goes when the process's last handle does."""
        if self._released:
            return
        self._released = True
        with _REGISTRY_LOCK:
            entry = _REGISTRY.get(self.path)
            if entry is None:
                return
            entry.count -= 1
            if entry.count == 0:
                del _REGISTRY[self.path]
                os.close(entry.fd)  # closing the descriptor drops the flock

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class _Refused:
    holder: Holder | None


def _try(key: Path, record: dict[str, object], *, reentrant: bool) -> _Refused | None:
    """One attempt: None when this process now holds ``key``, else who refused."""
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is not None:
            if entry.reentrant and reentrant:
                entry.count += 1
                return None
            return _Refused(holder(key))
        fd = _lock(key, record)
        if isinstance(fd, _Refused):
            return fd
        _REGISTRY[key] = _Held(fd=fd, reentrant=reentrant, count=1)
        return None


def _lock(path: Path, record: dict[str, object]) -> int | _Refused:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # non-inheritable (PEP 446)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        seen = _parse(os.pread(fd, _READ_BYTES, 0))
        os.close(fd)
        return _Refused(seen)
    except BaseException:
        os.close(fd)
        raise
    _write(fd, record)
    return fd


def _write(fd: int, record: dict[str, object]) -> None:
    body = {"pid": os.getpid(), "created": time.time(), **record}
    data = json.dumps(body).encode()
    os.pwrite(fd, data, 0)
    os.ftruncate(fd, len(data))


def _parse(raw: bytes) -> Holder | None:
    text = raw.decode(errors="replace").strip()
    if not text:
        return None
    if text.isdigit():  # v0.1 wrote the bare pid
        return Holder(pid=int(text))
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    created, label, meta = data.get("created"), data.get("label"), data.get("meta")
    return Holder(
        pid=data["pid"],
        created=float(created) if isinstance(created, int | float) else 0.0,
        label=label if isinstance(label, str) else None,
        meta={str(k): v for k, v in meta.items()} if isinstance(meta, dict) else {},
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["Holder", "LockHeldError", "ProcessLock", "Waiting", "held", "holder"]
