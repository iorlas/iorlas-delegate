"""Live event file for a dashboard: what a worker is doing right now (command, tool, message, tokens, stall), one JSON object per line.

Set DELEGATE_LIVE_FILE for a route.py call and every launcher appends normalised events to that path, flushed per line; unset, nothing is written.
Event = {"t": epoch, "k": kind, ...}. Kinds: start, cmd, cmd_end, tool, tool_end, msg, think, tokens, stall, end, raw (type name only).
The contract is read by the task-loop dashboard: do not change a kind or a field name without changing both. A write error warns once and stops writing;
nothing here ever raises into the call. Python 3.8, standard library only.
"""
import json
import os
import sys
import threading
import time

ENV = "DELEGATE_LIVE_FILE"
CMD_MAX, OUT_MAX, MSG_MAX, THINK_MAX = 2000, 1500, 2000, 500
STALL_FIRST_S = 60       # no event this long: one "stall", then one more every STALL_EVERY_S while it lasts
STALL_EVERY_S = 60
SHELL_TOOLS = ("bash", "shell")
ARG_KEYS = ("file_path", "filePath", "path", "pattern", "url", "query", "description", "command")      # a tool's main argument, first one present


def _s(x):
    return x if isinstance(x, str) else "" if x is None else str(x)


def _text_of(c):
    """A tool result's content (str, or a list of {"type": "text", "text"}) as one string."""
    if isinstance(c, list):
        return "\n".join(_s(b.get("text")) if isinstance(b, dict) else _s(b) for b in c)
    return _s(c)


def main_arg(inp):
    if isinstance(inp, dict):
        for k in ARG_KEYS:
            if inp.get(k):
                return _s(inp[k])
    return ""


def tool_text(name, inp):
    return (_s(name) + " " + main_arg(inp)).strip()[:CMD_MAX]


class Live:
    def __init__(self, cli, path=None, wall=time.time):
        path = os.environ.get(ENV) if path is None else path
        self.cli, self.wall, self.path = cli, wall, path or None
        self.fh, self.lock, self.broken = None, threading.Lock(), False
        self.t0 = wall()
        self.open = {}           # event id -> (started t, "cmd" | "tool")
        self.seen_usage = set()
        self.stall_at = STALL_FIRST_S
        self.on = bool(self.path)

    # -- writing ---------------------------------------------------------------------------------------------------------------------------
    def emit(self, k, **kw):
        if not self.on or self.broken:
            return
        try:
            with self.lock:
                if self.fh is None:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                    self.fh = open(self.path, "a", encoding="utf-8")
                ev = {"t": self.wall(), "k": k}
                ev.update({a: b for a, b in kw.items() if b is not None or a in ("exit",)})
                self.fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                self.fh.flush()
        except Exception as e:        # a dashboard feed must never break the call
            self.broken = True
            print(f"delegate: live event file {self.path} stopped: {e}", file=sys.stderr)

    # -- lifecycle -------------------------------------------------------------------------------------------------------------------------
    def start(self, lane=None, model=None, effort=None, role=None, pid=None):
        self.info = dict(harness=self.cli, lane=lane, model=model, effort=effort, role=role)
        if pid is not None:
            self.started(pid)

    def started(self, pid):
        """WORKER_STARTED callback: the process exists. The start event goes out here, once, with its pid."""
        if self.on and not getattr(self, "_started", False):
            self._started = True
            self.emit("start", pid=pid, **getattr(self, "info", {}))

    def poll(self, idle_s):
        """Called with the supervisor's seconds-without-events about every half second."""
        if not self.on:
            return
        if idle_s < self.stall_at - STALL_EVERY_S:       # events came back: the next silence starts at 60 again
            self.stall_at = STALL_FIRST_S
        if idle_s >= self.stall_at:
            self.emit("stall", secs=int(idle_s))
            while self.stall_at <= idle_s:
                self.stall_at += STALL_EVERY_S

    def end(self, status, cost_usd=None):
        if self.on:
            self.emit("end", status=status, secs=round(self.wall() - self.t0, 1), cost_usd=cost_usd)
            with self.lock:
                if self.fh:
                    try:
                        self.fh.close()
                    except Exception:
                        pass
                    self.fh = None

    # -- shared event helpers --------------------------------------------------------------------------------------------------------------
    def begin(self, ident, shell, text, cwd=None):
        if ident in self.open:
            return
        self.open[ident] = (self.wall(), "cmd" if shell else "tool")
        self.emit("cmd" if shell else "tool", id=ident, text=_s(text)[:CMD_MAX], cwd=cwd)

    def finish(self, ident, shell, text=None, exit_=None, out=None, secs=None):
        if ident not in self.open:
            self.begin(ident, shell, text or "")
        t, kind = self.open.pop(ident)
        secs = round(self.wall() - t, 2) if secs is None else round(secs, 2)
        if kind == "cmd":
            self.emit("cmd_end", id=ident, exit=exit_, out=_s(out)[-OUT_MAX:] if out else None, secs=secs)
        else:
            self.emit("tool_end", id=ident, out=_s(out)[-OUT_MAX:] if out else None, secs=secs)

    def tokens(self, i, cached, o):
        self.emit("tokens", **{"in": i, "cached": cached, "out": o})

    # -- per harness -----------------------------------------------------------------------------------------------------------------------
    def feed(self, ev):
        if not self.on or self.broken or not isinstance(ev, dict):
            return
        try:
            getattr(self, "_" + self.cli, self._raw)(ev)
        except Exception:
            self.emit("raw", type=_s(ev.get("type"))[:60])

    def _raw(self, ev):
        self.emit("raw", type=_s(ev.get("type"))[:60])

    def _codex(self, ev):
        kind, it = ev.get("type"), ev.get("item") if isinstance(ev.get("item"), dict) else {}
        ity, ident = it.get("type"), it.get("id")
        if kind in ("item.started", "item.completed", "item.updated") and ity:
            done = kind == "item.completed"
            if ity == "command_execution":
                if kind == "item.updated":
                    return
                if not done:
                    self.begin(ident, True, it.get("command"), it.get("cwd"))
                else:
                    self.finish(ident, True, it.get("command"), it.get("exit_code"), it.get("aggregated_output"))
            elif ity in ("mcp_tool_call", "file_change", "web_search", "collab_tool_call"):
                if kind == "item.updated":
                    return
                if ity == "mcp_tool_call":
                    text = _s(it.get("server")) + "." + _s(it.get("tool"))
                elif ity == "file_change":
                    ch = [c for c in it.get("changes") or [] if isinstance(c, dict)]
                    text = "edit " + ", ".join(_s(c.get("path")) for c in ch)
                else:
                    text = ity.split("_")[0] + " " + _s(it.get("query"))
                if done:
                    self.finish(ident, False, text.strip())
                else:
                    self.begin(ident, False, text.strip())
            elif ity == "agent_message" and done:
                self.emit("msg", text=_s(it.get("text"))[:MSG_MAX])
            elif ity == "reasoning" and done:
                self.emit("think", text=_s(it.get("text"))[:THINK_MAX])
            elif ity in ("error", "todo_list"):
                self._raw(ev)
            elif ity not in ("agent_message", "reasoning"):
                self._raw(ev)
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            self.tokens(u.get("input_tokens"), u.get("cached_input_tokens"), u.get("output_tokens"))
        elif kind not in ("thread.started", "turn.started", "turn.failed", "error"):
            self._raw(ev)

    def _claude(self, ev):
        kind = ev.get("type")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)] if isinstance(msg.get("content"), list) else []
        if kind == "assistant":
            for b in blocks:
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    self.emit("msg", text=_s(b["text"])[:MSG_MAX])
                elif bt == "thinking" and b.get("thinking"):
                    self.emit("think", text=_s(b["thinking"])[:THINK_MAX])
                elif bt == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if b.get("name") == "Bash":
                        self.begin(b.get("id"), True, inp.get("command"), ev.get("cwd"))
                    else:
                        self.begin(b.get("id"), False, tool_text(b.get("name"), inp))
            u, mid = msg.get("usage"), msg.get("id")
            if isinstance(u, dict) and mid not in self.seen_usage:
                self.seen_usage.add(mid)
                cached = u.get("cache_read_input_tokens") or 0
                self.tokens((u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0), cached, u.get("output_tokens"))
        elif kind == "user":
            for b in blocks:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in self.open:
                    self.finish(b["tool_use_id"], False, out=_text_of(b.get("content")), exit_=1 if b.get("is_error") else None)
        elif kind not in ("system", "result", "stream_event", "rate_limit_event"):
            self._raw(ev)

    def _opencode(self, ev):
        kind, p = ev.get("type"), ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if kind == "tool_use":
            st = p.get("state") if isinstance(p.get("state"), dict) else {}
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            ident, name = p.get("callID") or p.get("id"), _s(p.get("tool"))
            shell = name.lower() in SHELL_TOOLS
            text = inp.get("command") if shell else tool_text(name, inp)
            if st.get("status") in ("pending", "running"):
                self.begin(ident, shell, text)
            elif st.get("status") in ("completed", "error"):
                tm = st.get("time") if isinstance(st.get("time"), dict) else {}
                secs = (tm["end"] - tm["start"]) / 1000.0 if isinstance(tm.get("start"), (int, float)) and isinstance(tm.get("end"), (int, float)) else None
                md = st.get("metadata") if isinstance(st.get("metadata"), dict) else {}
                code = md.get("exit")
                self.finish(ident, shell, text, (code if isinstance(code, int) else 1 if st.get("status") == "error" else None) if shell else None,
                            st.get("output") or st.get("error"), secs)
            else:
                self._raw(ev)
        elif kind == "text" and p.get("text"):
            self.emit("msg", text=_s(p["text"])[:MSG_MAX])
        elif kind == "reasoning" and p.get("text"):
            self.emit("think", text=_s(p["text"])[:THINK_MAX])
        elif kind == "step_finish" and isinstance(p.get("tokens"), dict):
            u, c = p["tokens"], p["tokens"].get("cache") if isinstance(p["tokens"].get("cache"), dict) else {}
            self.tokens((u.get("input") or 0) + (c.get("read") or 0) + (c.get("write") or 0), c.get("read") or 0, (u.get("output") or 0) + (u.get("reasoning") or 0))
        elif kind not in ("step_start", "step_finish", "text", "reasoning", "error"):
            self._raw(ev)


def end_status(timed_out, hung, stop, rc):
    """run_proc's outcome as the contract's end status."""
    if timed_out:
        return "timeout"
    if hung or stop == "stall":
        return "killed"
    return "done" if stop == "answer" or rc == 0 else "error"
