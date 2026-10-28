"""CodexHarness: `codex exec --json`. Its argv, its event parser (each line parsed once, into HarnessEvents and Evidence), its transcript
file and its login status."""
import contextlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from delegate.adapters.harness.base import Evidence, LaunchInputs, LaunchSpec, Quiet, Summary
from delegate.contracts import events as ev

RECONNECT_RE = re.compile(r"^\s*Reconnecting\.\.\.\s*(\d+)/(\d+)")        # codex retrying its stream: progress until n == of
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOGIN_URL_RE = re.compile(r"https://\S+")
LOGIN_CODE_RE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")
WAITING_RE = re.compile(r"^\s*Reconnecting\.\.\.\s*waiting for network", re.I)     # codex waits for the network by itself
STATUS_RE = re.compile(r"(?:last status|status|HTTP)\W{0,3}(\d{3})\b")
SHELL_ITEM = "command_execution"
TOOL_ITEMS = ("mcp_tool_call", "file_change", "web_search", "collab_tool_call")
WORK_ITEMS = (SHELL_ITEM, "file_change", "agent_message", "mcp_tool_call")      # a finished one of these: the worker did something


def _s(x: object) -> str:
    return x if isinstance(x, str) else "" if x is None else str(x)


def _dict(x: object) -> dict:
    return x if isinstance(x, dict) else {}


def http_status(text: str) -> int | None:
    """The HTTP status a failure names: the JSON body codex forwards ({"status": 400}) or "last status: 429"."""
    try:
        body = json.loads(text)
        code = body.get("status") if isinstance(body, dict) else None
        if isinstance(code, int):
            return code
    except ValueError:
        pass
    m = STATUS_RE.search(text)
    return int(m.group(1)) if m else None


def tool_parts(item: dict) -> tuple[str, str]:
    """(name, arg) of a non-shell item: an MCP call is `server.tool`, an edit lists its paths, a search keeps its query."""
    kind = _s(item.get("type"))
    if kind == "mcp_tool_call":
        return _s(item.get("server")) + "." + _s(item.get("tool")), ""
    if kind == "file_change":
        return "edit", ", ".join(_s(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict))
    return kind.split("_")[0], _s(item.get("query"))


class CodexHarness(Quiet):
    kind = "codex"
    secret_event = ev.Cmd            # no hook under `codex exec`: stop the worker before it can use what it read
    EXE = "codex"

    def __init__(self, resume: str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.evidence = Evidence(ref=resume)
        self.out_file = ""

    # -- launch ----------------------------------------------------------------------------------------------------------------------------
    def launch_spec(self, i: LaunchInputs) -> LaunchSpec:
        self.out_file = i.out_file
        out = ["--json", "-o", i.out_file]
        schema = ["--output-schema", i.schema] if i.schema else []
        if i.resume:
            argv = [i.exe, "exec", "resume", "-m", i.model, "-c", f"model_reasoning_effort={i.effort}", "-c", f'sandbox_mode="{i.sandbox}"']
            argv += i.access + i.isolation + [x for x in i.cli if x == "--skip-git-repo-check"] + out + schema + [i.resume, "-"]
        else:
            argv = [i.exe] + i.cli[1:] + out + schema + ["-"]
        return LaunchSpec(argv=argv, env=i.env, cwd=i.cwd, stdin=i.prompt)

    # -- the event stream, parsed once -----------------------------------------------------------------------------------------------------
    def parse_line(self, line: str) -> list[ev.HarnessEvent]:
        try:
            raw = json.loads(line)
        except ValueError:
            return []
        if not isinstance(raw, dict):
            return []
        events = self._events(raw)
        self._note(raw, events)
        return events

    def _events(self, raw: dict) -> list[ev.HarnessEvent]:
        kind, item = _s(raw.get("type")), _dict(raw.get("item"))
        if kind in ("item.started", "item.completed", "item.updated") and item.get("type"):
            return self._item(kind, item)
        if kind == "turn.completed" and isinstance(raw.get("usage"), dict):
            u = raw["usage"]
            return [ev.TurnDone(usage=ev.Tokens(in_=u.get("input_tokens"), cached=u.get("cached_input_tokens"), out=u.get("output_tokens")))]
        if kind == "thread.started":
            return [ev.Started(ref=raw["thread_id"])] if raw.get("thread_id") else []
        if kind == "turn.started":
            return []
        if kind in ("turn.failed", "error"):
            return [self._failure(_s(_dict(raw.get("error")).get("message")) if kind == "turn.failed" else _s(raw.get("message")))]
        return [ev.Unknown(type=kind[:60])]

    def _item(self, kind: str, item: dict) -> list[ev.HarnessEvent]:
        ity, ident, done = item["type"], item.get("id"), kind == "item.completed"
        if ity == SHELL_ITEM:
            cmd = _s(item.get("command"))
            if kind == "item.updated":
                return []
            if not done:
                return [ev.Cmd(id=ident, text=cmd, cwd=item.get("cwd"))]
            code = item.get("exit_code")
            out: list[ev.HarnessEvent] = [ev.CmdEnd(id=ident, text=cmd, exit=code if isinstance(code, int) else None, out=item.get("aggregated_output"))]
            if item.get("status") == "declined":        # the reviewer refused an escalation
                out.append(ev.Denied(tool="Bash", input={"command": item.get("command")}))
            return out
        if ity in TOOL_ITEMS:
            name, arg = tool_parts(item)
            if kind == "item.updated":
                return []
            return [ev.ToolEnd(id=ident, name=name, arg=arg) if done else ev.Tool(id=ident, name=name, arg=arg)]
        if ity == "agent_message":
            return [ev.Msg(text=_s(item.get("text")))] if done else []
        if ity == "reasoning":
            return [ev.Think(text=_s(item.get("text")))] if done else []
        return [ev.Unknown(type=kind)]

    def _failure(self, text: str) -> ev.HarnessEvent:
        """codex's own reason for a failed turn or an error event; stderr then holds only transport noise."""
        if WAITING_RE.match(text):
            return ev.WaitingForNetwork(text=text)
        m = RECONNECT_RE.match(text)
        if m and int(m.group(1)) < int(m.group(2)):        # "Reconnecting... 2/5" is progress; 5/5 is the failure
            return ev.Reconnecting(n=int(m.group(1)), of=int(m.group(2)), text=text)
        return ev.HarnessError(text=text, http_status=http_status(text))

    def _note(self, raw: dict, events: list[ev.HarnessEvent]) -> None:
        """What this line established, for the classifier and the supervisor."""
        e, kind, item = self.evidence, _s(raw.get("type")), _dict(raw.get("item"))
        if kind.startswith(("item.", "turn.")) and kind != "turn.failed":
            e.net_at = None            # the stream moves again: the wait for the network is over
        if kind == "item.completed" and item.get("type") in WORK_ITEMS:
            e.work = True
        for x in events:
            if isinstance(x, ev.Started):
                e.ref = x.ref
            elif isinstance(x, ev.TurnDone) and x.usage:
                e.tokens = {"in": x.usage.in_, "cached": x.usage.cached, "out": x.usage.out}
                e.cumulative = dict(e.tokens)
                e.turns += 1
            elif isinstance(x, ev.Msg):
                e.last = x.text
            elif isinstance(x, ev.Denied):
                e.denials.append(x)
            elif isinstance(x, ev.HarnessError):
                e.error = x.text
            elif isinstance(x, (ev.Reconnecting, ev.WaitingForNetwork)):
                e.transient = x.text
                if isinstance(x, ev.WaitingForNetwork):
                    e.net_at = self.clock()

    def summary(self, rc: int | None, out: str, err: str, stop: str | None, timed_out: bool) -> Summary:
        """The answer is the file `-o` wrote at the turn's end, else the last agent message; the usage is raw (the caller subtracts a resumed thread's earlier total)."""
        e = self.evidence
        text = ""
        if self.out_file:
            p = Path(self.out_file)
            with contextlib.suppress(OSError):
                text = p.read_text(encoding="utf-8", errors="replace").strip()
                p.unlink()
        final = bool(text) or bool(e.last and (e.turns or stop == "answer"))        # the turn finished: codex writes -o only at its end
        text = text or e.last
        res = {"text": text, "ref": e.ref, "tokens": e.tokens, "turns": e.turns or None, "started": bool(e.ref), "partial": e.work, "answered": final,
               "cumulative": e.cumulative}
        return Summary(text=text, rc=rc, res=res, vendor=self.vendor_error(rc), stderr=err, work=e.work, stands=final and not e.error, tail=err or text)

    def vendor_error(self, rc: int | None) -> str:
        """The failure text the classifier reads: codex's error event, else a retry that never recovered (only when the exit was a failure)."""
        e = self.evidence
        return e.error or (e.transient if rc != 0 else "")

    # -- the world -------------------------------------------------------------------------------------------------------------------------
    def transcript(self, ref: str | None) -> str | None:
        """The rollout file of thread `ref`, or None."""
        if not ref:
            return None
        hits = sorted((Path.home() / ".codex" / "sessions").rglob(f"*{ref}.jsonl"))
        return str(hits[0]) if hits else None

    def login_probe(self, timeout: float) -> tuple[bool | None, str]:
        """(True | False | None, why) from `codex login status`: no model call, no quota; None = could not tell."""
        exe = shutil.which(self.EXE)
        if not exe:
            return None, f"{self.EXE} CLI not installed"
        try:
            r = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"{self.EXE} login check failed: {type(e).__name__}"
        text = r.stdout + r.stderr
        return r.returncode == 0 and "not logged" not in text.lower(), text.strip()[:120]

    @staticmethod
    def device_login_prompt(text: str) -> tuple[str | None, str | None]:
        """(link, code) printed by `codex login --device-auth`, either None while it has not printed it yet."""
        text = ANSI_RE.sub("", text)
        m, c = LOGIN_URL_RE.search(text), LOGIN_CODE_RE.search(text)
        return (m.group(0) if m else None), (c.group(0) if c else None)
