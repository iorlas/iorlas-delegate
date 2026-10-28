"""ClaudeHarness: `claude -p --output-format stream-json`. Its argv, its event parser (each line parsed once, into HarnessEvents and Evidence),
the usage arithmetic of its result, its transcript file and its login status."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from delegate.adapters import netprobe, procs
from delegate.adapters.harness.base import Evidence, LaunchInputs, LaunchSpec, Quiet, Summary
from delegate.contracts import events as ev

ARG_KEYS = ("file_path", "filePath", "path", "pattern", "url", "query", "description", "command")      # a tool's main argument, first one present
STREAM_USAGE = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
CLAUDE_ERROR_RE = re.compile(r"^(API Error|Credit balance|Invalid API key|Not logged in|Please run /login|Claude (AI )?usage limit|OAuth|Failed to authenticate"
                             r"|Request timed out|Error:)", re.I)       # how `claude -p` words its own failures; anything else in a result is the model talking
SUBSCRIPTION_TTL_S = 3600        # prompt-cache life: a -p main conversation on a subscription gets 1 h


def _s(x: object) -> str:
    return x if isinstance(x, str) else "" if x is None else str(x)


def _dict(x: object) -> dict:
    return x if isinstance(x, dict) else {}


def main_arg(inp: dict) -> str:
    return next((_s(inp[k]) for k in ARG_KEYS if inp.get(k)), "")


def text_of(c: object) -> str:
    """A tool result's content (str, or a list of {"type": "text", "text"}) as one string."""
    if isinstance(c, list):
        return "\n".join(_s(b.get("text")) if isinstance(b, dict) else _s(b) for b in c)
    return _s(c)


def input_of(u: dict) -> int:
    """Claude's input_tokens leaves out cache reads and cache writes; `in` counts all input, as codex's does."""
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)


def tokens_of(u: dict | None) -> dict | None:
    if not u:
        return None
    return {"in": input_of(u), "cached": u.get("cache_read_input_tokens") or 0, "out": u.get("output_tokens")}


def stream_usage(requests: list[dict]) -> dict:
    """The usage of each model request of a stream (newest wins per message id) -> one usage dict in the result event's shape. For a stream that
    ended without its `result` event (we stopped the worker): the requests already made were still paid for."""
    rs = [r for r in requests if isinstance(r, dict)]
    return {k: sum(r.get(k) or 0 for r in rs) for k in STREAM_USAGE} if rs else {}


def acct_of(u: dict | None, cost: float | None) -> dict | None:
    """`result` usage + total_cost_usd -> the call's accounting record. The cost is the harness's own figure on every lane; None when absent."""
    if not u and cost is None:
        return None
    u = u or {}
    by_ttl = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    a: dict[str, Any] = {"in_fresh": u.get("input_tokens") or 0, "cache_read": u.get("cache_read_input_tokens") or 0,
                         "cache_write": u.get("cache_creation_input_tokens") or 0, "out": u.get("output_tokens") or 0,
                         "cost_usd": None if cost is None else float(cost), "cost_source": "unknown" if cost is None else "harness",
                         "accounting": "estimated" if cost is not None else "unknown",
                         "ttl_s": SUBSCRIPTION_TTL_S, "ttl_source": "vendor_default"}
    if by_ttl:      # an unexpected downgrade to 5 m shows here
        a["cache_write_by_ttl"] = {"5m": by_ttl.get("ephemeral_5m_input_tokens") or 0, "1h": by_ttl.get("ephemeral_1h_input_tokens") or 0}
    return a


def vendor_error(j: dict) -> str:
    """The CLI's own error text of a result, else "". A failed result also carries the model's last words in the same field; only text worded
    like the CLI's errors (or an api_error_status beside it) counts as the vendor's."""
    res = j.get("result") if isinstance(j.get("result"), str) else ""
    if j.get("is_error") and res and (j.get("api_error_status") or CLAUDE_ERROR_RE.match(res.strip())):
        return res.strip()
    return ""


class ClaudeHarness(Quiet):
    kind = "claude"
    secret_event = None           # its PreToolUse hook blocks the call
    EXE = "claude"

    def __init__(self) -> None:
        self.evidence = Evidence()
        self.open: dict[Any, tuple[bool, str, str]] = {}        # tool_use id -> (shell, name, arg), until its tool_result
        self.seen_usage: set[Any] = set()

    # -- launch ----------------------------------------------------------------------------------------------------------------------------
    def launch_spec(self, i: LaunchInputs) -> LaunchSpec:
        tools = ["Read", "Glob", "Grep"] + (["Edit", "Write"] if i.write else []) + ["Bash"]       # read-only steps: Bash limited by the guard
        argv = [i.exe, "-p", "--output-format", "stream-json", "--verbose", "--model", i.model, "--effort", i.effort]
        argv += ["--resume", i.resume] if i.resume else ["--session-id", i.session]
        argv += ["--append-system-prompt", i.system, "--restricted", "--tools", ",".join(tools), "--strict-mcp-config",
                 "--permission-mode", "auto", "--permission-prompts", "none", "--settings", i.settings]
        argv += ["--json-schema", Path(i.schema).read_text(encoding="utf-8")] if i.schema else []
        self.evidence.ref = i.resume or i.session       # claude takes the session id from us: known before the first event
        return LaunchSpec(argv=argv, env=i.env, cwd=i.cwd, stdin=i.prompt)

    # -- the event stream, parsed once -----------------------------------------------------------------------------------------------------
    def parse_line(self, line: str) -> list[ev.HarnessEvent]:
        try:
            raw = json.loads(line)
        except ValueError:
            return []
        if not isinstance(raw, dict):
            return []
        kind, e = _s(raw.get("type")), self.evidence
        msg = _dict(raw.get("message"))
        blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)] if isinstance(msg.get("content"), list) else []
        if kind in ("assistant", "user", "result"):
            e.work = True           # the session exists on disk from its first message
        if kind == "assistant":
            return self._assistant(raw, msg, blocks)
        if kind == "user":
            return self._user(blocks)
        if kind == "result":
            return self._result(raw)
        return [] if kind in ("system", "stream_event", "rate_limit_event") else [ev.Unknown(type=kind[:60])]

    def _assistant(self, raw: dict, msg: dict, blocks: list[dict]) -> list[ev.HarnessEvent]:
        out: list[ev.HarnessEvent] = []
        tools = any(b.get("type") == "tool_use" for b in blocks)
        done = msg.get("stop_reason") == "end_turn" and not tools     # the final assistant message; a later event resets the grace
        for b in blocks:
            kind = b.get("type")
            if kind == "text" and b.get("text"):
                self.evidence.last = _s(b["text"])
                out.append(ev.Msg(text=self.evidence.last, final=done))
            elif kind == "thinking" and b.get("thinking"):
                out.append(ev.Think(text=_s(b["thinking"])))
            elif kind == "tool_use":
                out.append(self._tool(b, raw.get("cwd")))
        u = msg.get("usage")
        if isinstance(u, dict):          # one model request: its whole input is the context size
            self.evidence.context_k = round(input_of(u) / 1000, 1)
            if msg.get("id") not in self.seen_usage:
                self.seen_usage.add(msg.get("id"))
                self.evidence.steps.append(u)
                cached = u.get("cache_read_input_tokens") or 0
                out.append(ev.Tokens(in_=input_of(u), cached=cached, out=u.get("output_tokens"), cache_write=u.get("cache_creation_input_tokens")))
        return out

    def _tool(self, b: dict, cwd: object) -> ev.HarnessEvent:
        inp, ident = _dict(b.get("input")), b.get("id")
        if b.get("name") == "Bash":
            self.open[ident] = (True, "Bash", _s(inp.get("command")))
            return ev.Cmd(id=ident, text=_s(inp.get("command")), cwd=cwd if isinstance(cwd, str) else None)
        self.open[ident] = (False, _s(b.get("name")), main_arg(inp))
        return ev.Tool(id=ident, name=_s(b.get("name")), arg=main_arg(inp))

    def _user(self, blocks: list[dict]) -> list[ev.HarnessEvent]:
        out: list[ev.HarnessEvent] = []
        for b in blocks:
            ident = b.get("tool_use_id")
            if b.get("type") != "tool_result" or ident not in self.open:
                continue
            shell, name, arg = self.open.pop(ident)
            res = text_of(b.get("content"))
            out.append(ev.CmdEnd(id=ident, text=arg, exit=1 if b.get("is_error") else None, out=res) if shell
                       else ev.ToolEnd(id=ident, name=name, arg=arg, out=res))
        return out

    def _result(self, raw: dict) -> list[ev.HarnessEvent]:
        """The first result is the answer: claude -p then waits for background tasks and takes another turn on their exit."""
        e = self.evidence
        if e.final is not None:
            return []
        e.final = raw
        e.turns = raw.get("num_turns") or 0
        out: list[ev.HarnessEvent] = [ev.TurnDone(usage=None)]
        for x in raw.get("permission_denials") or []:        # a call the CLI's own classifier or our guard refused
            if isinstance(x, dict):
                d = ev.Denied(tool=_s(x.get("tool_name")), input=_dict(x.get("tool_input")))
                e.denials.append(d)
                out.append(d)
        return out

    # -- the finished stream ---------------------------------------------------------------------------------------------------------------
    def summary(self, rc: int | None, out: str, err: str, stop: str | None, timed_out: bool) -> Summary:
        e = self.evidence
        j = e.final or {}
        by_message = not e.final and stop == "answer" and bool(e.last)         # the final assistant message came, the result event never did
        if (e.final and not j.get("is_error")) or by_message:
            rc = 0          # stopped by us after answering
        text = j.get("result") if isinstance(j.get("result"), str) else e.last if by_message else out.strip()[-2000:]
        cost = float(j.get("total_cost_usd") or 0)          # the harness's own figure on every lane, subscription included (study 85)
        u = j.get("usage") if isinstance(j.get("usage"), dict) else stream_usage(e.steps)     # no result event: what the stream's requests reported
        failed = rc != 0 or bool(j.get("is_error"))
        res = {"text": text, "ref": j.get("session_id") or e.ref, "cost": cost, "tokens": tokens_of(u), "acct": acct_of(u, j.get("total_cost_usd")),
               "turns": j.get("num_turns"), "context_k": e.context_k, "started": e.work, "partial": e.work}
        warn = ["answer_done_process_alive"] if by_message else ["net_after_answer"] if e.final and netprobe.looks_dropped(err) else []     # ae6v: the answer was in first
        return Summary(text=text, rc=(rc or 1) if failed else 0, res=res, vendor=vendor_error(j), stderr=err, work=(j.get("num_turns") or 0) > 1,
                       check=failed or timed_out, warnings=warn, cost=cost, tail=text,
                       data=j.get("structured_output") if isinstance(j.get("structured_output"), (dict, list)) else None,
                       facts={"http_status": j.get("api_error_status")})

    def vendor_error(self, rc: int | None) -> str:
        return vendor_error(self.evidence.final or {})

    # -- the world -------------------------------------------------------------------------------------------------------------------------
    def transcript(self, ref: str | None) -> str | None:
        """The session file of `ref`, or None."""
        if not ref:
            return None
        hits = sorted((Path.home() / ".claude" / "projects").glob("*/" + ref + ".jsonl"))
        return str(hits[0]) if hits else None

    def login_probe(self, timeout: float) -> tuple[bool | None, str]:
        """(True | False | None, why) from `claude auth status`: no model call, no quota; None = could not tell."""
        exe = shutil.which(self.EXE)
        if not exe:
            return None, f"{self.EXE} CLI not installed"
        try:
            r = procs.run_capture([exe, "auth", "status"], timeout)
        except (OSError, subprocess.TimeoutExpired) as x:
            return None, f"{self.EXE} login check failed: {type(x).__name__}"
        try:
            logged_in = json.loads(r.stdout).get("loggedIn")
        except (ValueError, AttributeError):
            logged_in = None
        return (logged_in if isinstance(logged_in, bool) else None), (r.stdout + r.stderr).strip()[:120]
