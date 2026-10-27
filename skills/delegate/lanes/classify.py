"""The one outcome ladder (delegate-e05): what a vendor CLI said -> one reason from contract.json.

Pure: no state, no I/O except reading contract.json once. The three launchers (codex, opencode, claude) collect evidence, call
`classify`, then apply the effects of the reason in one place (route.settle). Vendor text enters through `vendor_err` (the vendor's own
error event, or the CLI's own error result) and `stderr`; the model's answer text is never an argument, so a worker that says
"rate limit" cannot take its lane out.
"""
import json
import re
from collections import namedtuple
from pathlib import Path

CONTRACT_FILE = Path(__file__).resolve().parent.parent / "contract.json"

# ---- PATTERNS: every regex that turns vendor text into a reason lives here --------------------------------------------------------
LIMIT_RE = re.compile(r"usage limit|rate.?limit|quota|too many requests|\b429\b|try again (later|in)|credit balance", re.I)
CREDIT_RE = re.compile(r"spend(ing)?[- ]?(limit|control)|out of credits|credit balance|insufficient (funds|credit)|credits? (exhausted|too low)|personal-team-blocked", re.I)
SPEND_RE = re.compile(CREDIT_RE.pattern + r"|top[- ]?up|billing|cycle cap|cap reached", re.I)      # cockpit: any prose that means money
LOGIN_RE = re.compile(r"not logged in|please (run|use) [`'\"]?\S*\s*login|oauth session expired|failed to authenticate|invalid api key|401 unauthorized", re.I)
NETWORK_RE = re.compile(r"ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|getaddrinfo|could not resolve host|network is unreachable"
                        r"|cannot connect to api|unable to connect", re.I)
CODEX_STATUS_RE = re.compile(r'"status":\s*4(?!29)\d\d')          # codex puts the HTTP status inside its error JSON; 429 is a limit, not a bad request
BAD_REQUEST_RE = re.compile(CODEX_STATUS_RE.pattern + r"|invalid_request_error|invalid_json_schema|Invalid schema for response_format|unrecognized arguments:|error: unknown argument", re.I)      # the vendor refused the request itself: schema, or a flag its CLI no longer takes
CLAUDE_ERROR_RE = re.compile(r"^(API Error|Credit balance|Invalid API key|Not logged in|Please run /login|Claude (AI )?usage limit|OAuth|Failed to authenticate"
                             r"|Request timed out|Error:)", re.I)       # how `claude -p` words its own failures; anything else in a result is the model talking
# Paths a worker must never touch: credentials outside the repo and .env files in it (.env.example and kin are fine). A tripwire for
# accidents and lazy injections, not a boundary: a determined worker can spell a path so it does not match.
SECRET_RE = re.compile(r"\.ssh\b|\.gnupg\b|\.aws\b|\.azure\b|\.config/gh\b|\.docker/config\.json|\.netrc\b|\.git-credentials\b|"
                       r"\.codex/auth\.json|Library/Keychains|(?<![\w.-])\.env(?:\.(?!(?:example|sample|template|dist)\b)[\w-]+)?(?![\w.-])")
PATTERNS = {"limit": LIMIT_RE, "credit": CREDIT_RE, "spend": SPEND_RE, "login": LOGIN_RE, "network": NETWORK_RE, "codex_status": CODEX_STATUS_RE,
            "bad_request": BAD_REQUEST_RE, "claude_error": CLAUDE_ERROR_RE, "secret": SECRET_RE}

Outcome = namedtuple("Outcome", "reason detail source")        # source: ours | event | stderr | exit


def load_contract():
    with open(CONTRACT_FILE, encoding="utf-8") as f:
        return json.load(f)


CONTRACT = load_contract()


def claude_vendor_error(j):
    """-> the CLI's own error text of a `claude -p` result, else "". A failed result also carries the model's last words in the same field;
    only text worded like the CLI's errors (or an api_error_status beside it) counts as the vendor's."""
    res = j.get("result") if isinstance(j.get("result"), str) else ""
    if j.get("is_error") and res and (j.get("api_error_status") or CLAUDE_ERROR_RE.match(res.strip())):
        return res.strip()
    return ""


def classify(rc, vendor_err="", stderr="", timed_out=False, work_seen=False, structured=None):
    """-> Outcome, or None when the run is done. First match wins.

    rc: exit code (negative = a signal); vendor_err: the vendor's error text from its event stream; stderr: the CLI's stderr; timed_out: our
    --timeout fired; work_seen: the worker did something before it failed; structured: facts we measured ourselves:
    {"secret": cmd, "watchdog": "limit"|"hung", "msg": log line, "http_status": int, "net_up": False}."""
    s = structured or {}
    ev, rc = (vendor_err or "").strip(), rc or 0
    if s.get("secret"):
        return Outcome("secrets", str(s["secret"]), "ours")
    if s.get("watchdog") == "limit":
        msg = s.get("msg") or ""
        return Outcome("credit_exhausted" if CREDIT_RE.search(msg) else "rate_limited", msg, "ours")
    if s.get("watchdog") == "hung":
        return Outcome("network" if s.get("net_up") is False else "hung", "", "ours")
    if timed_out:
        return Outcome("timeout", "", "ours")
    text = ev + ("\n" + (stderr or "") if rc != 0 else "")
    src = "event" if ev else "stderr"
    http = s.get("http_status")
    if isinstance(http, int) and 400 <= http < 500:
        if http == 429:
            return Outcome("rate_limited", text, src)
        if http in (401, 403):
            return Outcome("logged_out", text, src)
        return Outcome("bad_request", text, src)
    if CREDIT_RE.search(text):
        return Outcome("credit_exhausted", text, src)
    if LIMIT_RE.search(text):
        return Outcome("rate_limited", text, src)
    if LOGIN_RE.search(text):
        return Outcome("logged_out", text, src)
    if BAD_REQUEST_RE.search(text):
        return Outcome("bad_request", text, src)
    if NETWORK_RE.search(text) and not work_seen:
        return Outcome("network", text, src)
    if rc < 0 and not ev:
        return Outcome("killed", "signal %d" % -rc, "exit")
    if ev and not work_seen:
        return Outcome("vendor_blip", ev, "event")
    if ev:
        return Outcome("worker_failed", ev, "event")
    if rc != 0:
        return Outcome("worker_failed", (stderr or "").strip(), "stderr" if (stderr or "").strip() else "exit")
    return None


def facts(reason):
    """-> the contract row of a reason (scope, permanence, retry kind, health effect), or None for a name contract.json does not list."""
    return CONTRACT["reasons"].get(reason)


def benches_pair(reason):
    """True when the reason may count against a (lane, model) pair's health. bad_request is a caller fault: never (61 5.1)."""
    row = facts(reason)
    return bool(row) and row["health"] in ("breaker", "exhausted")


def stop_facts(reason, retry_after=None, fix=None):
    """-> the `stop` object an answer carries: {reason, scope, permanence, retry: {kind, at}}. An unknown reason still gets a row
    (open reasons, closed facts): machine / needs_owner / none."""
    row = facts(reason) or {"scope": "machine", "permanence": "needs_owner", "retry": "none"}
    perm, kind = row["permanence"], row["retry"]
    if retry_after and kind in ("none", "predicted"):       # a time the lane ends by itself: transient, whatever the table says for the general case
        perm, kind = "transient", "predicted"
    elif kind == "predicted":       # nothing to wait for: a person must act
        kind, perm = "none", "needs_owner"
    stop = {"reason": reason, "scope": row["scope"], "permanence": perm, "retry": {"kind": kind, "at": retry_after if kind in ("predicted", "poll") else None}}
    if fix:
        stop["fix"] = fix
    return stop
