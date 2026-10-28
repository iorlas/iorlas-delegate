"""Atomic file write — write to a temp file in the same directory, optionally fsync, os.replace.

The rename is atomic on POSIX, so a crash or a concurrent reader never observes a
partially-written or truncated file, and a failure before the replace leaves any
pre-existing target untouched. The temp name is unique per call, so concurrent writers
(threads or processes) never share a temp file: the last replace wins whole.

Three entry points share one core: :func:`atomic_write_bytes`, :func:`atomic_write_text`
and :func:`atomic_write_json`. All take ``fsync`` (default on) and ``keep_mode``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True, keep_mode: bool = False) -> None:
    """Atomically write ``data`` to ``path`` (tempfile + optional fsync + ``os.replace``).

    Args:
        path: The target. Missing parent directories are created.
        data: The exact bytes to store.
        fsync: Flush the temp file to disk before the replace (default). ``False`` keeps the
            atomic swap but not durability across power loss: right for a cache or a state
            file rewritten many times a second.
        keep_mode: ``True`` gives the new file the permission bits of the file it replaces;
            with no file to replace it gets the process default (``0o666`` minus the umask).
            ``False`` (default) leaves the temp file's own ``0o600``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _mode_for(path) if keep_mode else None
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            _ = fh.write(data)
            fh.flush()
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            if fsync:
                os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, fsync: bool = True, keep_mode: bool = False) -> None:
    r"""Atomically write ``text`` to ``path``; see :func:`atomic_write_bytes` for the options.

    Bytes on disk are exactly ``text.encode("utf-8")`` — no newline translation happens
    here. A caller reading the result back with Python's default text mode (e.g.
    ``path.read_text()``) will still see universal-newline translation applied on
    *read* (a lone ``\r`` becomes ``\n``); that is standard library behavior, not
    something this function does or can override.
    """
    atomic_write_bytes(path, text.encode("utf-8"), fsync=fsync, keep_mode=keep_mode)


def atomic_write_json(
    path: Path,
    value: object,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    fsync: bool = True,
    keep_mode: bool = False,
) -> None:
    """Atomically write ``value`` as JSON plus one trailing newline; options as :func:`atomic_write_bytes`.

    Non-ASCII text is written as is (UTF-8), never ``\\u``-escaped. A value ``json`` cannot
    encode raises ``TypeError`` before anything is touched, so the prior target survives.
    """
    body = json.dumps(value, indent=indent, sort_keys=sort_keys, ensure_ascii=False) + "\n"
    atomic_write_text(path, body, fsync=fsync, keep_mode=keep_mode)


def _mode_for(path: Path) -> int:
    """The permission bits of ``path``, or the process default when it does not exist."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        _ = os.umask(umask)
        return 0o666 & ~umask


__all__ = ["atomic_write_bytes", "atomic_write_json", "atomic_write_text"]
