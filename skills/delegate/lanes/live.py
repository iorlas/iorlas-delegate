"""Live event feed for Claude and OpenCode (until their harness classes land in D3): their stream-json events become the same live lines
Codex's HarnessEvents do. The file, the kinds, the cap and the WorkerStarted line are delegate.app.publish.LiveSink and contracts/live.py.
Codex has its own parser (adapters/harness/codex.py) and never comes through here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from delegate.app.publish import CMD_MAX, LiveSink  # noqa: E402

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


class Live(LiveSink):
    def __init__(self, cli, path=None, run_id=""):
        super().__init__(cli, path, run_id=run_id)
        self.seen_usage = set()

    def feed(self, ev):
        if not self.on or self.broken or not isinstance(ev, dict):
            return
        try:
            getattr(self, "_" + self.harness, self._raw)(ev)
        except Exception:
            self.raw(_s(ev.get("type")))

    def _raw(self, ev):
        self.raw(_s(ev.get("type")))

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
            self.message(_s(p["text"]))
        elif kind == "reasoning" and p.get("text"):
            self.think(_s(p["text"]))
        elif kind == "step_finish" and isinstance(p.get("tokens"), dict):
            u, c = p["tokens"], p["tokens"].get("cache") if isinstance(p["tokens"].get("cache"), dict) else {}
            self.tokens((u.get("input") or 0) + (c.get("read") or 0) + (c.get("write") or 0), c.get("read") or 0, (u.get("output") or 0) + (u.get("reasoning") or 0))
        elif kind not in ("step_start", "step_finish", "text", "reasoning", "error"):
            self._raw(ev)
