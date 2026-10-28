"""The one outcome ladder : what a vendor CLI said -> one reason from contract.json.

Pure: no state, no I/O except reading contract.json once. The three launchers (codex, opencode, claude) collect evidence, call
`classify`, then apply the effects of the reason in one place (Settler.apply). Vendor text enters through `vendor_err` (the vendor's own
error event, or the CLI's own error result) and `stderr`; the model's answer text is never an argument, so a worker that says
"rate limit" cannot take its lane out.
"""
import re
from typing import NamedTuple

# ---- PATTERNS: every regex that turns vendor text into a reason lives here --------------------------------------------------------
LIMIT_RE = re.compile(r"usage limit|rate.?limit|quota|too many requests|\b429\b|try again (later|in)|credit balance", re.I)
CREDIT_RE = re.compile(r"spend(ing)?[- ]?(limit|control)|out of credits|credit balance|insufficient (funds|credit)|credits? (exhausted|too low)|personal-team-blocked", re.I)
SPEND_RE = re.compile(CREDIT_RE.pattern + r"|top[- ]?up|billing|cycle cap|cap reached", re.I)      # cockpit: any prose that means money
LOGIN_RE = re.compile(r"not logged in|please (run|use) [`'\"]?\S*\s*login|oauth session expired|failed to authenticate|invalid api key|401 unauthorized", re.I)
NETWORK_RE = re.compile(r"ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|getaddrinfo|could not resolve host|network is unreachable"
                        r"|cannot connect to api|unable to connect", re.I)
CODEX_STATUS_RE = re.compile(r'"status":\s*4(?!29)\d\d')          # codex puts the HTTP status inside its error JSON; 429 is a limit, not a bad request
BAD_REQUEST_RE = re.compile(CODEX_STATUS_RE.pattern + r"|invalid_request_error|invalid_json_schema|Invalid schema for response_format|unrecognized arguments:|error: unknown argument", re.I)      # the vendor refused the request itself: schema, or a flag its CLI no longer takes
PATTERNS = {"limit": LIMIT_RE, "credit": CREDIT_RE, "spend": SPEND_RE, "login": LOGIN_RE, "network": NETWORK_RE, "codex_status": CODEX_STATUS_RE,
            "bad_request": BAD_REQUEST_RE}

class Outcome(NamedTuple):
    reason: str
    detail: str
    source: str        # ours | event | stderr | exit


def classify(rc: int | None, vendor_err: str | None = "", stderr: str | None = "", timed_out: bool = False, work_seen: bool = False,
             structured: dict | None = None):
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


def says_login(text: str):
    return bool(LOGIN_RE.search(text))


def says_spend(text: str):
    return bool(SPEND_RE.search(text))


def limit_words(text: str, top: str | None):
    """The window a limit message names ("weekly limit hit"), else `top` (the fullest window's name) or the plain words."""
    if re.search(r"free usage", text, re.I):
        return "free usage limit reached"
    for pat, word in ((r"week|7.?day", "weekly"), (r"5.?hour|five.?hour", "5-hour"), (r"daily|per day|\bday\b", "daily")):
        if re.search(pat, text, re.I):
            return f"{word} limit hit"
    return f"{top} limit hit" if top else "usage limit hit"
