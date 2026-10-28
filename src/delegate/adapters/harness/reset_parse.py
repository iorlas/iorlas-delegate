"""Limit prose to a reset time: when a vendor message states when a limit ends, and the guess when it does not. Regex over vendor text, so it
lives with the harnesses (adapters). Pure: the caller passes `t` (now)."""
import contextlib
import datetime as dt
import re
import time
from collections.abc import Callable

RESET_LADDER_MIN = (15, 30, 60)       # a limit whose end nobody states: refusal time + this, one rung further for each consecutive guess
RESET_AT_RE = re.compile(r"(?:reset(?:s)?|resume(?:s)?|available|renews?|until|try again|retry)\s*(?:at|on|after|:)?\s*"
                         r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d)?)(?:\.\d+)?\s*(Z\b|UTC\b|GMT\b|[+-]\d\d:?\d\d\b)?", re.I)
RETRY_AFTER_RE = re.compile(r"retry[- ]after\W{0,3}\s*(\d+(?:\.\d+)?)\b(?!\s*[-:/]\d)|retry[- ]after\W{0,3}\s*([A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} [\d:]{8} GMT)", re.I)
IN_RE = re.compile(r"\bin\s+(?:about\s+|around\s+)?(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b(?:\s*(?:and\s*)?(\d+)\s*(minutes?|mins?|m)\b)?", re.I)
TRY_AT_RE = re.compile(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", re.I)
UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _ok(at: float | None, t: float) -> float | None:
    """`at` when it is a plausible reset time: after now and within 40 days."""
    return at if at and t < at <= t + 40 * 86400 else None


def parse_reset(msg: str | None, t: float):
    """-> (epoch, zone) when a vendor message states when a limit ends, else (None, None). Forms, in order: `Retry-After: 120` or an HTTP date;
    `will reset at 2026-10-25 14:49:00` / ISO after reset|until|try again (a stated offset or Z/UTC is kept; none = this machine's zone, since the
    vendor shows its user local time); `try again at 5:50 AM` (local, the next such time); `in 5 hours` / `in 2h 30m`. A time already past, or
    more than 40 days off, is not a reset time."""
    msg = msg or ""
    m = RETRY_AFTER_RE.search(msg)
    if m:
        at = _ok(t + float(m.group(1)), t) if m.group(1) else None
        if m.group(2):
            with contextlib.suppress(TypeError, ValueError):
                at = _ok(dt.datetime.strptime(m.group(2), "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.UTC).timestamp(), t)
        if at:
            return at, None
    m = RESET_AT_RE.search(msg)
    if m:
        day, clock, zone = m.group(1), m.group(2), (m.group(3) or "")
        z = zone.upper().replace("GMT", "UTC")
        with contextlib.suppress(ValueError):
            naive = dt.datetime.fromisoformat(f"{day}T{clock if clock.count(':') == 2 else clock + ':00'}")
            if not z:
                at = _ok(naive.timestamp(), t)          # naive = this machine's local zone
                if at:
                    return at, "local " + (time.tzname[time.localtime(at).tm_isdst > 0] or "")
            else:
                off = dt.UTC if z in ("Z", "UTC") else dt.timezone(dt.timedelta(hours=int(z[:3]), minutes=int(z[0] + z[-2:])))
                at = _ok(naive.replace(tzinfo=off).timestamp(), t)
                if at:
                    return at, "UTC" if z in ("Z", "UTC") else z
    m = TRY_AT_RE.search(msg)
    if m:
        h, mi = int(m.group(1)) % 12 if m.group(3) else int(m.group(1)), int(m.group(2))
        h += 12 if (m.group(3) or "").upper() == "PM" else 0
        at = dt.datetime.fromtimestamp(t).replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if at.timestamp() <= t:
            at += dt.timedelta(days=1)
        return at.timestamp(), "local " + (time.tzname[time.localtime(at.timestamp()).tm_isdst > 0] or "")
    m = IN_RE.search(msg)
    if m:
        secs = float(m.group(1)) * UNIT_S[m.group(2)[0].lower()] + (int(m.group(3)) * 60 if m.group(3) else 0)
        at = _ok(t + secs, t)
        if at:
            return at, None
    return None, None


def limit_until(msg: str | None, t: float, d: dict, reset: Callable[[], float | None] | None = None, guess_n: int = 0):
    """-> (until, source, zone) for a limit refusal. source: `vendor` = the message (or the vendor's own usage reading, `reset()`) states the end;
    `guess` = the lane's soonest window reset, else the refusal time + 15/30/60 min by how many guesses in a row (guess_n)."""
    at, zone = parse_reset(msg, t)
    if at:
        return at, "vendor", zone
    at = reset() if reset else None
    if at and at > t:
        return at, "vendor", None
    at = d.get("limit_reset")
    if at and at > t:
        return at, "guess", None
    return t + RESET_LADDER_MIN[min(guess_n, len(RESET_LADDER_MIN) - 1)] * 60, "guess", None
