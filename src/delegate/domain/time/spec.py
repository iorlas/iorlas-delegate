"""Times: epoch seconds everywhere; text only at the edges. `+2h` / `+3d` / ISO / epoch in, 'Sat 24 Oct 18:00' and '2h30m' out."""
import datetime as dt

WEEK_MIN = 7 * 24 * 60
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNIT_S = {"m": 60, "h": 3600, "d": 86400}


def fmt_in(secs: float) -> str:
    """'2h 10m', '6d 21h', '45m'."""
    s = int(secs)
    if s < 60:
        return "<1m"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


def local(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%a %d %b %H:%M")


def when(epoch: float, t: float) -> str:
    return f"{local(epoch)} (in {fmt_in(epoch - t)})"


def to_epoch(v: object) -> float | None:
    """epoch seconds, epoch ms, or an ISO string -> epoch seconds; None if unparsable."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_until(v: object, t: float) -> float | None:
    """'+90m' / '+2h' / '+3d' relative to t; else an epoch, epoch ms or ISO time."""
    s = str(v or "").strip()
    if len(s) > 2 and s[0] == "+" and s[-1] in UNIT_S and s[1:-1].isdigit():
        return t + int(s[1:-1]) * UNIT_S[s[-1]]
    try:
        v = float(s)
    except ValueError:
        pass
    return to_epoch(v)


def next_weekly(spec: str, t: float) -> float | None:
    """'Sun 17:00' (local time) -> epoch of the next such moment, or None."""
    try:
        day, hm = spec.split()
        hh, mm = (int(x) for x in hm.split(":"))
        target = DAYS.index(day[:3].lower())
    except (ValueError, AttributeError):
        return None
    base = dt.datetime.fromtimestamp(t)
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0) + dt.timedelta(days=(target - base.weekday()) % 7)
    if cand.timestamp() <= t:
        cand += dt.timedelta(days=7)
    return cand.timestamp()
