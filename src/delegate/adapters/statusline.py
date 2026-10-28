"""Claude Code's status line hands over the subscription's rate limits; `route.py capture-limits` stores them in ~/.claude/state/max-limits.json
and this module is the one reader and writer of that file."""
import json
import os
from pathlib import Path


def path() -> Path:
    return Path.home() / ".claude" / "state" / "max-limits.json"


def read(t: float, stale_s: float) -> tuple[dict, float] | None:
    """-> ({five_hour, weekly} each {used, resets_at}, when it was written), or None when the file is missing, stale or has no number."""
    try:
        with open(path(), encoding="utf-8") as fh:
            d = json.load(fh)
        updated = float(d.get("updated", 0))
        if t - updated > stale_s:
            return None
        rl = d.get("rate_limits") or {}
        usage = {k: {"used": float(w["used_percentage"]), "resets_at": w.get("resets_at")}
                 for k, w in (("five_hour", rl.get("five_hour") or {}), ("weekly", rl.get("seven_day") or {})) if w.get("used_percentage") is not None}
        return (usage, updated) if usage else None
    except (OSError, ValueError, TypeError):
        return None


def write(rate_limits: dict, t: float) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"updated": t, "rate_limits": rate_limits}), encoding="utf-8")
    os.replace(tmp, p)
