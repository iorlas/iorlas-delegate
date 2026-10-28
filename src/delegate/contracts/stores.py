"""The files the router owns under the config dir, each behind one class: StateStore, HistoryStore, LedgerStore, CheckpointStore.

Every write is atomic (temp file + replace) and private (0600) and, where two processes may race, under `locked`. A file that is missing,
unreadable or of another shape reads as empty: a store never raises on read and never migrates.
"""
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from atomic_io import atomic_write_text
from jsonl_log import append_line, open_append
from process_lock import LockHeldError, ProcessLock

from delegate.contracts import codec
from delegate.contracts.history import ROWS, HealthCheckpoint, HealthState, HistoryRow, RunRow
from delegate.contracts.routing import BreakerPolicy
from delegate.contracts.state import Profile, StateDoc

HISTORY_MAX, HISTORY_DAYS = 1_000_000, 14


@contextlib.contextmanager
def locked(path: Path | str, timeout: float = 10.0) -> Iterator[None]:
    """An exclusive lock on `path` (process_lock on a sibling `.lock` file; threads of this process exclude each other too, the kernel frees a dead
    holder's). TimeoutError when it stays held for `timeout` s."""
    try:
        lock = ProcessLock.acquire(Path(str(path) + ".lock"), timeout=timeout, reentrant=False)
    except LockHeldError as e:
        raise TimeoutError("%s is locked by another route.py for more than %.0fs" % (path, timeout)) from e
    try:
        yield
    finally:
        lock.release()


def append_json(path: Path, value: object) -> None:
    """One JSON line appended with a single O_APPEND write (jsonl_log), so writers in other processes never interleave."""
    fd = open_append(path)
    try:
        append_line(fd, (json.dumps(value) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def private(path: Path | str) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def write_atomic(path: Path, text: str) -> None:
    atomic_write_text(path, text, fsync=False)      # temp file + replace, private (0600); a state file is rewritten often, so no fsync
    private(path)


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_STATE_WARNED: set[Path] = set()


class StateStore:
    """`state.json` as a `StateDoc`. All writes go through `transaction()` / `update()`: one lock, read-modify-write."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StateDoc:
        data = read_json(self.path)
        try:
            return codec.from_json(StateDoc, data)
        except codec.CodecError as e:
            if data is not None and self.path not in _STATE_WARNED:      # a file from before the typed state: dropped, never migrated
                _STATE_WARNED.add(self.path)
                print(f"delegate: {self.path} is not a v{StateDoc.V} state file ({e}); starting from empty state", file=sys.stderr)
            return StateDoc()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[StateDoc]:
        with locked(self.path):
            doc = self.load()
            yield doc
            write_atomic(self.path, json.dumps(codec.to_json(doc)))

    def update(self, fn: Callable[[StateDoc], object]) -> StateDoc:
        with self.transaction() as doc:
            fn(doc)
        return doc


class HistoryStore:
    """`history.jsonl`: append a row, read the rows after a time, find a ref's last thread total. A damaged line is skipped."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, row: HistoryRow) -> None:
        """Never fails a run."""
        with contextlib.suppress(Exception), locked(self.path):
            if self.path.exists() and self.path.stat().st_size > HISTORY_MAX:
                self._rotate(time.time() - HISTORY_DAYS * 86400)
            append_json(self.path, codec.to_json(row))
            private(self.path)

    def _rotate(self, cut: float) -> None:
        keep = [r for r in self._read(0.0) if r[0].ts >= cut]
        atomic_write_text(self.path, "".join(ln for _, ln in keep), fsync=False)      # shelf lacks: jsonl_log compacts by size and file count, not by row age

    def _read(self, since: float) -> list[tuple[HistoryRow, str]]:
        out = []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return out
        for ln in lines:
            try:
                row = codec.from_tagged(ROWS, json.loads(ln))
            except ValueError:      # CodecError is a ValueError: a stale or corrupt line
                continue
            if row.ts > since:
                out.append((row, ln + "\n"))
        return out

    def rows(self, since: float = 0.0) -> list[HistoryRow]:
        return [r for r, _ in self._read(since)]

    def runs(self, n: int) -> list[RunRow]:
        """The last n finished runs, newest last."""
        return [r for r in self.rows() if isinstance(r, RunRow)][-n:]

    def last_cumulative(self, ref: str) -> dict | None:
        """The thread total the newest row for `ref` recorded: where the rollout file is gone, a resume still knows what came before."""
        for r in reversed(self.rows()):
            if isinstance(r, RunRow) and r.ref == ref and r.cumulative:
                return r.cumulative
        return None


class CheckpointStore:
    """`health-checkpoint.json`: a cache of the health fold. Missing, damaged or built under another breaker policy: no checkpoint."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, policy: BreakerPolicy) -> HealthState | None:
        try:
            cp = codec.from_json(HealthCheckpoint, read_json(self.path))
        except codec.CodecError:
            return None
        return cp.state if cp.policy == policy else None

    def save(self, state: HealthState, policy: BreakerPolicy) -> None:
        with contextlib.suppress(OSError):
            write_atomic(self.path, json.dumps(codec.to_json(HealthCheckpoint(policy=policy, state=state))))


class ConfigStore:
    """`config.json`: the owner's overrides. Top-level keys other than `profile` are settings overrides (deep-merged over routing.json by the
    caller); `profile` is the owner's lane choices and never leaves this class as a setting."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict:
        d = read_json(self.path)
        return d if isinstance(d, dict) else {}

    def overrides(self) -> dict:
        return {k: v for k, v in self.read().items() if k != "profile"}

    def update(self, fn: Callable[[dict], object]) -> dict:
        """Locked read-modify-write: callers never save a stale snapshot."""
        with locked(self.path):
            cur = self.read()
            fn(cur)
            write_atomic(self.path, json.dumps(cur, indent=2) + "\n")
        return cur

    def profile(self) -> Profile:
        try:
            return codec.from_json(Profile, self.read().get("profile") or {})
        except codec.CodecError:
            return Profile()

    def _put_profile(self, fn: Callable[[Profile], object]) -> None:
        def put(cur: dict) -> None:
            try:
                p = codec.from_json(Profile, cur.get("profile") or {})
            except codec.CodecError:
                p = Profile()
            fn(p)
            cur["profile"] = codec.to_json(p)
        self.update(put)

    def set_lane(self, name: str, value: str | None) -> None:
        """value None removes the entry (back to auto-detect)."""
        def put(p: Profile) -> None:
            p.lanes.pop(name, None) if value is None else p.lanes.__setitem__(name, value)
        self._put_profile(put)

    def mark_offered(self, names: list[str]) -> None:
        self._put_profile(lambda p: p.offered.extend(n for n in names if n not in p.offered))
