"""Per-call usage and cost accounting (delegate-uk0, study 85). Pure functions: the launchers feed them what their harness reported.

An accounting record `acct` is {in_fresh, cache_read, cache_write, out, cost_usd, cost_source, accounting, ttl_s, ttl_source, cache_write_by_ttl?}.
Counts are per call, never cumulative. cache_write and cost_usd are None where the vendor does not report them. No price table here:
cost is what the harness returned, or unknown. warm_until is telemetry (when the vendor's prompt cache probably expires); nothing routes on it."""
import datetime as dt

CLAUDE_SUBSCRIPTION_TTL_S = 3600      # a -p main conversation on a subscription gets the 1 h cache
CLAUDE_CREDIT_TTL_S = 300             # API / credit billing: the 5 min default
CODEX_TTL_S = 3600                    # measured 65 min (study 85); the vendor states 5-10 min to 24 h


def per_call(total, before):
    """A running thread total ({in, cached, out}) minus the total the thread held before this call. `codex exec resume` reports the
    running total; a total lower than before is not a running total of that thread, so it stands as given."""
    if not total or not before or (total.get("in") or 0) < before["in"]:
        return total
    return {k: max(0, (total.get(k) or 0) - before[k]) for k in before}


def last_cumulative(rows, ref):
    """The thread total a history row recorded for `ref` (newest wins), or None."""
    for r in reversed(list(rows)):
        if r.get("ref") == ref and isinstance(r.get("cumulative"), dict):
            return r["cumulative"]
    return None


def codex(tokens):
    """Per-call {in, cached, out} (already net of a resumed thread's earlier total) -> acct. Codex returns no cost."""
    t = tokens or {}
    cached = t.get("cached") or 0
    return {"in_fresh": max(0, (t.get("in") or 0) - cached), "cache_read": cached, "cache_write": None, "out": t.get("out") or 0,
            "cost_usd": None, "cost_source": "unknown", "accounting": "exact", "ttl_s": CODEX_TTL_S, "ttl_source": "observed"} if tokens else None


def claude(u, cost, credit):
    """`claude -p` result usage + total_cost_usd -> acct. The cost is the harness's own figure on every lane; None when absent."""
    if not u and cost is None:
        return None
    u = u or {}
    by_ttl = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    a = {"in_fresh": u.get("input_tokens") or 0, "cache_read": u.get("cache_read_input_tokens") or 0, "cache_write": u.get("cache_creation_input_tokens") or 0,
         "out": u.get("output_tokens") or 0, "cost_usd": None if cost is None else float(cost), "cost_source": "unknown" if cost is None else "harness",
         "accounting": "estimated" if cost is not None else "unknown",
         "ttl_s": CLAUDE_CREDIT_TTL_S if credit else CLAUDE_SUBSCRIPTION_TTL_S, "ttl_source": "vendor_default"}
    if by_ttl:      # an unexpected downgrade to 5 m shows here
        a["cache_write_by_ttl"] = {"5m": by_ttl.get("ephemeral_5m_input_tokens") or 0, "1h": by_ttl.get("ephemeral_1h_input_tokens") or 0}
    return a


def claude_stream(requests):
    """The assistant events of a `claude -p` stream (one usage dict per model request, newest wins per message id) -> one usage dict in the result event's shape.
    Used where the stream ended without its `result` event (we stopped the worker): the requests already made were still paid for."""
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    rs = [r for r in requests if isinstance(r, dict)]
    return {k: sum(r.get(k) or 0 for r in rs) for k in keys} if rs else {}


def opencode_step(acct, tokens, cost):
    """Fold one opencode step-finish (tokens {input, output, reasoning, cache{read,write}}, cost) into the running acct."""
    c = tokens.get("cache") or {}
    a = acct or {"in_fresh": 0, "cache_read": 0, "cache_write": 0, "out": 0, "cost_usd": None, "ttl_s": None, "ttl_source": "unknown"}
    a["in_fresh"] += tokens.get("input") or 0
    a["cache_read"] += c.get("read") or 0
    a["cache_write"] += c.get("write") or 0
    a["out"] += (tokens.get("output") or 0) + (tokens.get("reasoning") or 0)
    if isinstance(cost, (int, float)):
        a["cost_usd"] = (a["cost_usd"] or 0.0) + float(cost)
    return a


def opencode_close(acct, lane):
    """Name the cost's source once the steps are in. Grok: opencode's catalog price per step. zai: its steps carry 0 and study 81 found
    that unverifiable, so unknown. Free DeepSeek: free."""
    if not acct:
        return None
    if lane == "ZAI_CODING_SUBSCRIPTION":
        acct.update(cost_usd=None, cost_source="unknown", accounting="unknown")
    elif lane == "DEEPSEEK_FREE":
        acct.update(cost_usd=0.0, cost_source="free", accounting="estimated")
    elif acct["cost_usd"] is None:
        acct.update(cost_source="unknown", accounting="unknown")
    else:
        acct.update(cost_source="harness", accounting="estimated")
    return acct


def merge(a, b):
    """Two calls of one run (the schema re-ask) -> one acct; unknown cost stays unknown only if both are."""
    if not a or not b:
        return a or b
    out = dict(b)
    for k in ("in_fresh", "cache_read", "out"):
        out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    out["cache_write"] = None if a.get("cache_write") is None and b.get("cache_write") is None else (a.get("cache_write") or 0) + (b.get("cache_write") or 0)
    costs = [x["cost_usd"] for x in (a, b) if x.get("cost_usd") is not None]
    out["cost_usd"] = sum(costs) if costs else None
    if costs and out["cost_source"] == "unknown":
        out["cost_source"] = "harness"
    return out


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fields(acct, ref, ended_at):
    """-> the additive `usage` keys for a finished call (acct may be None: a call that reported nothing)."""
    a = acct or {}
    ttl = a.get("ttl_s") if ref else None
    return {"ref": ref, "last_activity_at": iso(ended_at) if ref else None,
            "warm_until": iso(ended_at + ttl) if ttl else None, "ttl_s": ttl or None, "ttl_source": a.get("ttl_source") if ttl else "unknown",
            "cost_usd": a.get("cost_usd"), "cost_source": a.get("cost_source") or "unknown", "accounting": a.get("accounting") or "unknown"}


def token_fields(acct):
    """The per-call counts, added to usage.tokens beside the old in/cached/out."""
    if not acct:
        return {}
    out = {k: acct.get(k) for k in ("in_fresh", "cache_read", "cache_write", "out")}
    if acct.get("cache_write_by_ttl"):
        out["cache_write_by_ttl"] = acct["cache_write_by_ttl"]
    return out
