"""LiveSink: HarnessEvent -> LiveEvent lines on $DELEGATE_LIVE_FILE (contracts/live.py), flushed per line.

Unset, nothing is written. The first line of the file is the WorkerStarted of the first worker; each worker's `start` event follows it.
A write error warns once and stops the feed; a file past LIVE_FILE_MAX stops growing (only `end` still goes out). Nothing here raises into the call.
"""
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from delegate.contracts import events as ev
from delegate.contracts import live
from delegate.contracts.codec import ABSENT, to_json
from delegate.contracts.live import LIVE_FILE_MAX

ENV = "DELEGATE_LIVE_FILE"
CMD_MAX, OUT_MAX, MSG_MAX, THINK_MAX = 2000, 1500, 2000, 500
STALL_FIRST_S = 60       # no event this long: one "stall", then one more every STALL_EVERY_S while it lasts
STALL_EVERY_S = 60


def _opt(x: Any) -> Any:
    return ABSENT if x is None else x


def _text(name: str, arg: str) -> str:
    return (name + " " + arg).strip()[:CMD_MAX]


class LiveSink:
    def __init__(self, harness: str, path: str | None = None, wall: Callable[[], float] = time.time, run_id: str = "") -> None:
        path = os.environ.get(ENV) if path is None else path
        self.harness, self.wall, self.path, self.run_id = harness, wall, path or None, run_id
        self.fh: Any = None
        self.lock = threading.Lock()
        self.broken = self.capped = False
        self.t0 = wall()
        self.open: dict[Any, tuple[float, str]] = {}        # event id -> (started t, "cmd" | "tool")
        self.stall_at = STALL_FIRST_S
        self.on = bool(self.path)
        self.info: dict[str, Any] = {}
        self.began = False

    # -- writing ---------------------------------------------------------------------------------------------------------------------------
    def write(self, rec: Any, final: bool = False) -> None:
        """One record (a LiveEvent or the WorkerStarted) as one line."""
        if not self.on or self.broken or (self.capped and not final):
            return
        try:
            with self.lock:
                if self.fh is None:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                    self.fh = open(self.path, "a", encoding="utf-8")
                line = json.dumps(to_json(rec), ensure_ascii=False) + "\n"
                if not final and self.fh.tell() + len(line.encode("utf-8")) > LIVE_FILE_MAX:
                    self.capped = True
                    return
                self.fh.write(line)
                self.fh.flush()
        except Exception as e:        # a dashboard feed must never break the call
            self.broken = True
            print(f"delegate: live event file {self.path} stopped: {e}", file=sys.stderr)

    def _first_line(self) -> bool:
        try:
            return os.path.getsize(self.path) == 0
        except OSError:
            return True

    # -- lifecycle -------------------------------------------------------------------------------------------------------------------------
    def start(self, lane: str | None = None, model: str | None = None, effort: str | None = None, role: str | None = None) -> None:
        self.info = dict(lane=_opt(lane), model=_opt(model), effort=_opt(effort), role=_opt(role))

    def started(self, pid: int) -> None:
        """WORKER_STARTED callback: the process exists. A new file gets the WorkerStarted first; the start event follows, once per worker."""
        if not self.on or self.began:
            return
        self.began = True
        if self._first_line():
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = pid
            self.write(live.WorkerStarted(pid=pid, pgid=pgid, run_id=self.run_id))
        self.write(live.LiveStart(t=self.wall(), harness=self.harness, pid=pid, **self.info))

    def poll(self, idle_s: float) -> None:
        """Called with the supervisor's seconds-without-events about every half second."""
        if not self.on:
            return
        if idle_s < self.stall_at - STALL_EVERY_S:       # events came back: the next silence starts at 60 again
            self.stall_at = STALL_FIRST_S
        if idle_s >= self.stall_at:
            self.write(live.LiveStall(t=self.wall(), secs=int(idle_s)))
            while self.stall_at <= idle_s:
                self.stall_at += STALL_EVERY_S

    def end(self, status: str, cost_usd: float | None = None) -> None:
        if not self.on:
            return
        self.write(live.LiveEnd(t=self.wall(), status=status, secs=round(self.wall() - self.t0, 1), cost_usd=_opt(cost_usd)), final=True)
        with self.lock:
            if self.fh:
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = None

    # -- paired events ---------------------------------------------------------------------------------------------------------------------
    def begin(self, ident: Any, shell: bool, text: str, cwd: str | None = None, prov: bool = False) -> None:
        if ident in self.open:
            return
        self.open[ident] = (self.wall(), "cmd" if shell else "tool")
        if shell:
            self.write(live.LiveCmdStart(t=self.wall(), id=str(ident), text=str(text)[:CMD_MAX], cwd=_opt(cwd), prov=True if prov else ABSENT))
        else:
            self.write(live.LiveToolStart(t=self.wall(), id=str(ident), text=str(text)[:CMD_MAX], prov=True if prov else ABSENT))

    def finish(self, ident: Any, shell: bool, text: str | None = None, exit_: int | None = None, out: Any = None, secs: float | None = None) -> None:
        if ident not in self.open:
            self.begin(ident, shell, text or "")
        t, kind = self.open.pop(ident)
        secs = round(self.wall() - t, 2) if secs is None else round(secs, 2)
        tail = str(out)[-OUT_MAX:] if out else ABSENT
        if kind == "cmd":
            self.write(live.LiveCmdEnd(t=self.wall(), id=str(ident), exit=exit_, secs=secs, out=tail))
        else:
            self.write(live.LiveToolEnd(t=self.wall(), id=str(ident), secs=secs, out=tail))

    def tokens(self, i: int | None, cached: int | None, o: int | None) -> None:
        self.write(live.LiveTokens(t=self.wall(), in_=_opt(i), cached=_opt(cached), out=_opt(o)))

    def message(self, text: str) -> None:
        self.write(live.LiveMsg(t=self.wall(), text=text[:MSG_MAX]))

    def think(self, text: str) -> None:
        self.write(live.LiveThink(t=self.wall(), text=text[:THINK_MAX]))

    def raw(self, type_: str) -> None:
        self.write(live.LiveRaw(t=self.wall(), type=type_[:60]))

    def retry(self, n: int | None = None, of: int | None = None, why: str | None = None) -> None:
        self.write(live.LiveRetry(t=self.wall(), n=_opt(n), of=_opt(of), why=_opt(why and why[:200])))

    # -- a harness's parsed events ---------------------------------------------------------------------------------------------------------
    def feed(self, e: ev.HarnessEvent) -> None:
        if not self.on or self.broken:
            return
        match e:
            case ev.Cmd():
                self.begin(e.id, True, e.text, e.cwd)
            case ev.CmdEnd():
                self.finish(e.id, True, e.text, e.exit, e.out, e.secs)
            case ev.Tool():
                self.begin(e.id, False, _text(e.name, e.arg), prov=e.prov)
            case ev.ToolEnd():
                self.finish(e.id, False, _text(e.name, e.arg), None, e.out, e.secs)
            case ev.Msg():
                self.message(e.text)
            case ev.Think():
                self.think(e.text)
            case ev.TurnDone(usage=u) if u is not None:
                self.tokens(u.in_, u.cached, u.out)
            case ev.Tokens():
                self.tokens(e.in_, e.cached, e.out)
            case ev.Reconnecting():
                self.retry(e.n, e.of, e.text)
            case ev.HarnessError(http_status=429):
                self.retry(why="http 429")
            case ev.Unknown():
                self.raw(e.type)
            case ev.TurnDone():
                self.raw("turn.completed")


def end_status(timed_out: bool, hung: str | None, stop: str | None, rc: int | None) -> str:
    """run_proc's outcome as the contract's end status."""
    if timed_out:
        return "timeout"
    if hung or stop == "stall":
        return "killed"
    return "done" if stop == "answer" or rc == 0 else "error"
