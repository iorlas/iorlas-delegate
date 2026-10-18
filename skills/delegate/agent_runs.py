"""Publish delegate's worker runs and lane limits as agent-runs v1 records, for dashboards (task-loop's among them).

The format is a public contract, not delegate's state: github.com/iorlas/iorlas-task-loop,
docs/contracts/agent-runs-v1.md. Keep this file the only place that writes it; never fail a run because of it.
Call sites, and what a rewrite must keep: docs/dev/integrations.md.
"""

import json
import os
import time
from pathlib import Path

RETAIN = 7 * 86400


def root():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-runs"


def _write(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)


def run(run_id, **fields):
    """Create or update one run record (v1). Call with the required fields first, then with ended/status/exit."""
    try:
        f = root() / "runs" / (run_id + ".json")
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {"v": 1, "producer": "delegate", "id": run_id}
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


def lanes(items, now):
    """items: [(name, out_until or None, why or None, [(window, used_pct, resets_at), ...]), ...]"""
    try:
        _write(root() / "lanes" / "delegate.json", {
            "v": 1, "producer": "delegate", "at": now,
            "lanes": [{"name": n, "out_until": until, "why": why,
                       "usage": [{"window": w, "used_pct": u, "resets_at": r} for w, u, r in usage if u is not None]}
                      for n, until, why, usage in items]})
    except OSError:
        pass
