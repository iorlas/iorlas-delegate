"""OpenCodeHarness: `opencode run --format json --print-logs`. Its argv and sandboxed environment, its event parser (stdout events, and the
permission lines of its log on stderr that start a tool call seconds before stdout reports it), the usage arithmetic, its exported transcript and
its login status."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, override

from delegate.adapters import procs
from delegate.adapters.harness import opencode_sandbox as box
from delegate.adapters.harness.base import Evidence, LaunchInputs, LaunchSpec, Quiet, Summary
from delegate.contracts import events as ev
from delegate.contracts.codec import record

FINAL_REASONS = ("stop", "end_turn", "end-turn")       # step_finish part.reason of a step that ends the turn (not "tool-calls")
SHELL_TOOLS = ("bash", "shell")
ARG_KEYS = ("file_path", "filePath", "path", "pattern", "url", "query", "description", "command")
# The log line OpenCode prints the moment a tool starts: `... service=permission evaluated permission=bash pattern="sleep 6" action=allow`.
PERMISSION_RE = re.compile(r'evaluated permission=(\S+) pattern=("(?:[^"\\]|\\.)*"|\S+)')
ECHO_RE = re.compile(r"\b(?:permission|pattern|input|command|args)=")
NOT_TOOLS = ("external_directory",)
ENV_OFF = {"OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
           "OPENCODE_DISABLE_SHARE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"}
SESSION_VARS = ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_JSON", "ZHIPU_API_KEY", "ZAI_API_KEY")


@record
class Profile:
    """What one lane adds to the generic OpenCode run."""

    providers: tuple[str, ...] = ()    # enabled_providers: the only ones that may be used (empty: OpenCode's own choice)
    own_data: bool = False         # a data and cache folder of its own, so no saved login or key can be spent
    price: str = "catalog"         # "catalog" (the step's cost is the catalog price), "free", "unknown" (steps carry 0 and it cannot be verified)
    auth_key: str = ""             # the entry in OpenCode's auth file that proves the login ("" = no login needed)
    need_key: bool = False


def _s(x: object) -> str:
    return x if isinstance(x, str) else "" if x is None else str(x)


def _dict(x: object) -> dict:
    return x if isinstance(x, dict) else {}


def tool_text(name: str, inp: dict) -> str:
    return next((_s(inp[k]) for k in ARG_KEYS if inp.get(k)), "")


def acct_close(acct: dict | None, price: str) -> dict | None:
    """Name the cost's source once the steps are in. catalog: opencode's price per step. unknown: its steps carry 0 and that is unverifiable. free: free."""
    if not acct:
        return None
    if price == "unknown":
        acct.update(cost_usd=None, cost_source="unknown", accounting="unknown")
    elif price == "free":
        acct.update(cost_usd=0.0, cost_source="free", accounting="estimated")
    elif acct["cost_usd"] is None:
        acct.update(cost_source="unknown", accounting="unknown")
    else:
        acct.update(cost_source="harness", accounting="estimated")
    return acct


def acct_fold(steps: list[dict]) -> dict | None:
    """The step_finish records (tokens {input, output, reasoning, cache{read,write}}, cost) -> one running accounting record."""
    acct: dict[str, Any] | None = None
    for st in steps:
        u, c = st["tokens"], _dict(st["tokens"].get("cache"))
        a = acct or {"in_fresh": 0, "cache_read": 0, "cache_write": 0, "out": 0, "cost_usd": None, "ttl_s": None, "ttl_source": "unknown"}
        a["in_fresh"] += u.get("input") or 0
        a["cache_read"] += c.get("read") or 0
        a["cache_write"] += c.get("write") or 0
        a["out"] += (u.get("output") or 0) + (u.get("reasoning") or 0)
        if isinstance(st.get("cost"), (int, float)):
            a["cost_usd"] = (a["cost_usd"] or 0.0) + float(st["cost"])
        acct = a
    return acct


class OpenCodeHarness(Quiet):
    kind = "opencode"
    secret_event = ev.CmdEnd        # a declined or failed call still ran no further than its end row

    def __init__(self, profile: Profile | None = None, root: Path | None = None) -> None:
        self.profile, self.root = profile or Profile(), root
        self.evidence = Evidence()
        self.exe = ""
        self.shell: Path | None = None
        self.prov: list[tuple[str, str, str]] = []        # open provisional rows: (id, tool, pattern)
        self.n = 0

    # -- launch ----------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def find_exe() -> str | None:
        return shutil.which("opencode") or next((str(p) for p in [Path.home() / ".opencode" / "bin" / "opencode"] if p.is_file()), None)

    @staticmethod
    def supported() -> bool:
        return sys.platform == "darwin"         # the command sandbox is sandbox-exec

    def launch_spec(self, i: LaunchInputs) -> LaunchSpec:
        """Writes the sandboxed shell this run's commands use (`cleanup` removes it). The env is `i.env` plus OpenCode's isolation."""
        assert self.root is not None
        self.exe = i.exe
        home = self.root / "opencode-home"       # an empty config root: the user's ~/.config/opencode (MCP servers, providers, permissions) stays out
        home.mkdir(parents=True, exist_ok=True)
        self.shell = self.root / f"opencode-shell-{i.session[:8]}.sh"
        self.write_shell(i.cwd, i.write, self.shell)
        config: dict[str, Any] = {"permission": box.permissions(i.write)}
        env = dict(i.env, SHELL=str(self.shell), XDG_CONFIG_HOME=str(home), **ENV_OFF)
        if self.profile.own_data:
            env["XDG_DATA_HOME"], env["XDG_CACHE_HOME"] = str(self.root / "opencode-free-data"), str(self.root / "opencode-free-cache")
        if self.profile.providers:
            for key in SESSION_VARS:
                env.pop(key, None)
            config["enabled_providers"] = list(self.profile.providers)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        argv = [i.exe, "run", "--pure", "--format", "json", "--print-logs", "--log-level", "INFO", "-m", i.model, "--variant", i.effort, "--dir", i.cwd]
        argv += ["--session", i.resume] if i.resume else []
        self.evidence.ref = i.resume
        return LaunchSpec(argv=argv, env=env, cwd=i.cwd, stdin=i.prompt)

    write_shell = staticmethod(box.write_shell)

    @property
    @override
    def log_dir(self) -> Path:
        return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "log"

    @override
    def echoes_input(self, line: str) -> bool:
        """A log line that quotes the worker's own tool input (`evaluated permission=bash pattern="..."`): its words are the worker's, not OpenCode's."""
        return bool(ECHO_RE.search(line))

    @override
    def cleanup(self) -> None:
        if self.shell:
            self.shell.unlink(missing_ok=True)

    def free_model_ok(self, model: str, env: dict[str, str]) -> bool:
        """Fail closed if the exact free model disappears or its catalog costs change."""
        if model != "opencode/deepseek-v4.1-flash-free":
            return False
        try:
            x = subprocess.run([self.exe, "models", "opencode", "--verbose"], env=env, capture_output=True, text=True, timeout=30)
            marker = model + "\n"
            if x.returncode or marker not in x.stdout:
                return False
            obj, _ = json.JSONDecoder().raw_decode(x.stdout.split(marker, 1)[1].lstrip())
            cost = obj.get("cost") or {}
            cache = cost.get("cache") or {}
            return (obj.get("providerID") == "opencode" and obj.get("id") == model.split("/", 1)[1] and obj.get("status") == "active"
                    and all(type(v) in (int, float) and v == 0 for v in (cost.get("input"), cost.get("output"), cache.get("read"), cache.get("write"))))
        except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
            return False

    # -- the event stream, parsed once -----------------------------------------------------------------------------------------------------
    def parse_line(self, line: str) -> list[ev.HarnessEvent]:
        try:
            raw = json.loads(line)
        except ValueError:
            return []
        if not isinstance(raw, dict):
            return []
        out: list[ev.HarnessEvent] = []
        e, kind, part = self.evidence, _s(raw.get("type")), _dict(raw.get("part"))
        if raw.get("sessionID") and not e.ref:
            e.ref = raw["sessionID"]
            out.append(ev.Started(ref=e.ref))
        if kind == "tool_use":
            out += self._tool(part)
        elif kind == "text" and part.get("text"):
            e.last, e.work = _s(part["text"]), True
            out.append(ev.Msg(text=e.last, final=False))
        elif kind == "reasoning" and part.get("text"):
            out.append(ev.Think(text=_s(part["text"])))
        elif kind == "step_finish":
            out += self._step(part)
        elif kind == "error":
            err = _dict(raw.get("error"))
            e.error = _s(_dict(err.get("data")).get("message") or err.get("name") or err)
            out.append(ev.HarnessError(text=e.error))
        elif kind not in ("step_start", "text", "reasoning"):
            out.append(ev.Unknown(type=kind[:60]))
        return out

    def _step(self, part: dict) -> list[ev.HarnessEvent]:
        e, u = self.evidence, part.get("tokens")
        if not isinstance(u, dict):
            return [ev.TurnDone(usage=None)] if part.get("reason") in FINAL_REASONS else []
        c = _dict(u.get("cache"))
        whole = (u.get("input") or 0) + (c.get("read") or 0) + (c.get("write") or 0)
        tk = e.tokens or {"in": 0, "cached": 0, "out": 0}
        e.tokens = {"in": (tk["in"] or 0) + whole, "cached": (tk["cached"] or 0) + (c.get("read") or 0),
                    "out": (tk["out"] or 0) + (u.get("output") or 0) + (u.get("reasoning") or 0)}
        e.steps.append({"tokens": u, "cost": part.get("cost")})
        e.turns += 1
        e.context_k = round(whole / 1000, 1)
        t = ev.Tokens(in_=whole, cached=c.get("read") or 0, out=(u.get("output") or 0) + (u.get("reasoning") or 0))
        return [ev.TurnDone(usage=t)] if part.get("reason") in FINAL_REASONS else [t]

    def _tool(self, part: dict) -> list[ev.HarnessEvent]:
        st, inp = _dict(part.get("state")), _dict(_dict(part.get("state")).get("input"))
        ident, name = part.get("callID") or part.get("id"), _s(part.get("tool"))
        shell, status = name.lower() in SHELL_TOOLS, st.get("status")
        self.evidence.work = True
        text = _s(inp.get("command")) if shell else tool_text(name, inp)
        if status in ("pending", "running"):
            return [ev.Cmd(id=ident, text=text) if shell else ev.Tool(id=ident, name=name, arg=text)]
        if status not in ("completed", "error"):
            return [ev.Unknown(type="tool_use")]
        tm, md = _dict(st.get("time")), _dict(st.get("metadata"))
        secs = (tm["end"] - tm["start"]) / 1000.0 if isinstance(tm.get("start"), (int, float)) and isinstance(tm.get("end"), (int, float)) else None
        code = md.get("exit")
        out_text = st.get("output") or st.get("error")
        closed = self._close_prov(name, text, shell)
        ids = closed or [(ident, text)]
        if closed:
            secs = None         # timed from the provisional start, which is when the tool began
        res: list[ev.HarnessEvent] = []
        for k, (i, t) in enumerate(ids):
            o = out_text if k == 0 else None
            res.append(ev.CmdEnd(id=i, text=text if k == 0 else t, exit=code if isinstance(code, int) else 1 if status == "error" else None, out=o, secs=secs)
                       if shell else ev.ToolEnd(id=i, name=name, arg=text, out=o, secs=secs))
        res += self._denials(name, inp, _s(out_text))
        return res

    def _close_prov(self, tool: str, full: str, shell: bool) -> list[tuple[str, str]]:
        """The provisional rows this completed call closes: a bash line is logged once per sub-command, so every row of the tool whose pattern the
        command contains; else the oldest row of the tool."""
        rows = [r for r in self.prov if r[1] == tool]
        hit = [r for r in rows if shell and r[2] and r[2] in full] or rows[:1]
        for r in hit:
            self.prov.remove(r)
        return [(r[0], r[2]) for r in hit]

    def _denials(self, tool: str, inp: dict, out: str) -> list[ev.HarnessEvent]:
        if "prevents you from using this specific tool call" in out:
            d = ev.Denied(tool=tool, input=inp, kind="policy")
        elif "Operation not permitted" in out and not box.network_refusal(out):
            d = ev.Denied(tool=tool, input=inp, kind="sandbox_limit")
        else:
            return []
        self.evidence.denials.append(d)
        return [d]

    @override
    def parse_log_line(self, line: str) -> list[ev.HarnessEvent]:
        """A permission line of OpenCode's log is the earliest sign a tool started: a provisional Cmd or Tool, closed by the completed tool_use."""
        m = PERMISSION_RE.search(line)
        if not m or m.group(1) in NOT_TOOLS:
            return []
        tool, pat = m.group(1), m.group(2)
        if pat.startswith('"'):
            try:
                pat = json.loads(pat)
            except ValueError:
                pat = pat.strip('"')
        self.n += 1
        ident = f"oc:{self.n}"
        self.prov.append((ident, tool, pat))
        return [ev.Cmd(id=ident, text=pat, prov=True) if tool.lower() in SHELL_TOOLS else ev.Tool(id=ident, name=tool, arg=pat, prov=True)]

    # -- the finished stream ---------------------------------------------------------------------------------------------------------------
    def summary(self, rc: int | None, out: str, err: str, stop: str | None, timed_out: bool) -> Summary:
        e = self.evidence
        text = e.last.strip()
        acct = acct_close(acct_fold(e.steps), self.profile.price)
        res = {"text": text, "ref": e.ref, "tokens": e.tokens, "turns": e.turns or None, "context_k": e.context_k, "started": bool(e.ref), "partial": e.work,
               "acct": acct, "cost": (acct or {}).get("cost_usd") or 0}       # no `answered`: opencode has no resume-tested finished-answer signal (62-spikes-resume.md)
        why = e.error or (err if rc != 0 and stop != "answer" else "")       # stderr after we stopped it ourselves is the shell reporting our kill
        return Summary(text=text, rc=rc, res=res, vendor=why, stderr="", work=e.work, stands=bool(text) and not e.error, cost=float(res["cost"]), tail=err or text)

    def vendor_error(self, rc: int | None) -> str:
        return self.evidence.error

    # -- the world -------------------------------------------------------------------------------------------------------------------------
    def transcript(self, ref: str | None) -> str | None:
        """The session exported after the run, or None."""
        f = self.root / "opencode-sessions" / f"{ref}.json" if ref and self.root else None
        return str(f) if f and f.is_file() else None

    @override
    def export(self, env: dict[str, str]) -> None:
        """OpenCode keeps sessions in its database: export one to a file, so `transcript` is a path like the other harnesses'."""
        ref = self.evidence.ref
        if not ref or self.root is None:
            return
        try:
            x = procs.run_capture([self.exe, "export", ref], 60, env=env)
            if x.returncode == 0 and x.stdout.strip():
                f = self.root / "opencode-sessions" / f"{ref}.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(x.stdout, encoding="utf-8")
                os.chmod(f, 0o600)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def login_probe(self, timeout: float) -> tuple[bool | None, str]:
        """(True | False | None, why) from OpenCode's auth file: no model call, no quota."""
        if not self.find_exe():
            return None, "opencode CLI not installed"
        key = self.profile.auth_key
        if not key:
            return True, "free access needs no login"        # the zero-price check runs at launch
        f = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"
        try:
            v = json.loads(f.read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError, TypeError, AttributeError):
            v = None
        ok = bool(v) and (not self.profile.need_key or (isinstance(v, dict) and bool(v.get("key"))))
        return ok, f"{key} login in opencode's auth file" if ok else f"no {key} login in opencode's auth file"
