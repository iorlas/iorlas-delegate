"""The stop facts of an answer: contract.json's reasons table (scope, permanence, retry kind, health effect) and the `stop` object built from a row."""
import json
from pathlib import Path
from typing import Any

CONTRACT_FILE = Path(__file__).resolve().parents[3] / "skills" / "delegate" / "contract.json"       # next to src/ in the plugin tree and in a copy of it


def load_contract():
    with open(CONTRACT_FILE, encoding="utf-8") as f:
        return json.load(f)


CONTRACT = load_contract()


def facts(reason: str | None):
    """-> the contract row of a reason (scope, permanence, retry kind, health effect), or None for a name contract.json does not list."""
    return CONTRACT["reasons"].get(reason)


def benches_pair(reason: str | None):
    """True when the reason may count against a (lane, model) pair's health. bad_request is a caller fault: never (61 5.1)."""
    row = facts(reason)
    return bool(row) and row["health"] in ("breaker", "exhausted")


def stop_facts(reason: str | None, retry_after: float | None = None, fix: str | None = None):
    """-> the `stop` object an answer carries: {reason, scope, permanence, retry: {kind, at}}. An unknown reason still gets a row
    (open reasons, closed facts): machine / needs_owner / none."""
    row = facts(reason) or {"scope": "machine", "permanence": "needs_owner", "retry": "none"}
    perm, kind = row["permanence"], row["retry"]
    if retry_after and kind in ("none", "predicted"):       # a time the lane ends by itself: transient, whatever the table says for the general case
        perm, kind = "transient", "predicted"
    elif kind == "predicted":       # nothing to wait for: a person must act
        kind, perm = "none", "needs_owner"
    stop: dict[str, Any] = {"reason": reason, "scope": row["scope"], "permanence": perm, "retry": {"kind": kind, "at": retry_after if kind in ("predicted", "poll") else None}}
    if fix:
        stop["fix"] = fix
    return stop
