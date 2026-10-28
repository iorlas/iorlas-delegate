"""Publish delegate's worker runs and lane limits as agent-runs v1 records, for dashboards (task-loop's among them).

The format is a public contract, not delegate's state: github.com/iorlas/iorlas-task-loop,
docs/contracts/agent-runs-v1.md. Keep this file the only place that writes it; never fail a run because of it.
Call sites, and what a rewrite must keep: docs/dev/integrations.md.
"""

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app_dirs import config_dir
from atomic_io import atomic_write_json

RETAIN = 7 * 86400


def root():
    return config_dir("agent-runs")


def _write(path: Path, rec: dict):
    atomic_write_json(path, rec, fsync=False, keep_mode=True)


def run(run_id: str, **fields: Any):
    """Create or update one run record (v1). Call with the required fields first, then with ended/status/exit."""
    try:
        f = root() / "runs" / (run_id + ".json")
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec: dict[str, Any] = {"v": 1, "producer": "delegate", "id": run_id}
        rec.update({k: v for k, v in fields.items() if v is not None})
        _write(f, rec)
        cut = time.time() - RETAIN
        for old in f.parent.glob("*.json"):
            try:
                if old.stat().st_mtime < cut:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def lanes(items: Iterable[tuple[str, float | None, str | None, Iterable[tuple[str, float | None, float | None]]]], now: float):
    """items: [(name, out_until or None, why or None, [(window, used_pct, resets_at), ...]), ...]"""
    try:
        _write(root() / "lanes" / "delegate.json", {
            "v": 1, "producer": "delegate", "at": now,
            "lanes": [{"name": n, "out_until": until, "why": why,
                       "usage": [{"window": w, "used_pct": u, "resets_at": r} for w, u, r in usage if u is not None]}
                      for n, until, why, usage in items]})
    except OSError:
        pass
