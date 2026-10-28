"""One worker, end to end: the harness for a runner kind builds its launch (`prepare_*`), the Supervisor runs it, the one ladder classifies it and the
Settler applies the outcome (`launch_worker`). Also the rules every worker's prompt starts with and the guard hook it runs under."""
import contextlib
import dataclasses
import json
import shlex
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from delegate.adapters import agent_runs, codex_account
from delegate.adapters.harness import classify, opencode_sandbox
from delegate.adapters.harness.base import Harness, LaunchInputs, LaunchSpec
from delegate.adapters.harness.claude import ClaudeHarness
from delegate.adapters.harness.codex import CodexHarness
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile
from delegate.app import answer, decide, env, harnesses, net, settle, watch
from delegate.app.publish import LiveSink
from delegate.app.supervisor import LogWatcher, RunHooks, Supervisor
from delegate.contracts.state import StateDoc
from delegate.domain.cost import accounting
from delegate.domain.holds.book import HoldBook
from delegate.domain.policy import worker as policy
from delegate.domain.policy.matchers import secret_hit
from delegate.domain.policy.worker import EDIT_RULE, GIT_RULES, SECRET_RULE

DENIED_RULE = "If a command is denied, that is a decision for the owner: do not retry or reword it; report the exact command in your final message and stop."
NO_SUBAGENTS_RULE = "Do not spawn sub-agents; do the work yourself."
ESCALATE_RULE = ("You run unattended. If a command fails because of the sandbox (network, files outside this directory, a system resource), "
                 "request escalated permissions for that exact command once, with a one-line justification.")
PREAMBLE = (SECRET_RULE + " You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate skill. "
            + GIT_RULES + " " + EDIT_RULE + " " + DENIED_RULE + " Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran.")


def settler():
    return settle.Settler(env.states(), net.net_up, env.now, decide.busy_jitter)


def guard_settings(read_only: bool | None, engine_only: tuple[str, ...] = ()):
    """--settings for claude -p: delegate's PreToolUse hook (route.py guard), read-only for steps that may not write."""
    argv = [sys.executable, str(env.SKILL_DIR / "route.py"), "guard"] + (["--read-only"] if read_only else [])
    argv += [f"--engine-only={x}" for x in engine_only] if not read_only else []
    cmd = shlex.join(argv)
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}}


def codex_reset_now(t: float):
    """The reset of the Codex window a fresh reading shows full (>= 95%), else None."""
    res, _ = codex_account.rate_limits()
    rl = (res or {}).get("rateLimits") or {}
    full = [r for w in (rl.get("primary") or {}, rl.get("secondary") or {}) if (w.get("usedPercent") or 0) >= 95 and (r := w.get("resetsAt") or 0) > t]
    return min(full) if full else None


def repo_instruction_text(cwd: str, cap: int = 32768):
    """--restricted keeps Claude from loading the repo's CLAUDE.md; Codex reads AGENTS.md. Both lanes get the same files (repo_instructions),
    capped like Codex's project_doc_max_bytes."""
    out, left = "", cap
    for f in decide.repo_instructions(cwd):
        with contextlib.suppress(OSError):
            body = f.read_text(encoding="utf-8", errors="replace")[:left]
            left -= len(body)
            out += f"\n\nRepository instructions ({f.name} in {f.parent}):\n{body}"
        if left <= 0:
            break
    return out


class Prep(NamedTuple):
    """The harness, its launch, and what the ladder needs from before the launch."""

    hx: Harness
    spec: LaunchSpec
    extra: dict


def lane_gone_check(lane: str, since: float):
    """-> a function: the words of a limit, credit or login mark another process set on `lane` after `since` (wall clock), else None. The worker
    still running there is burning an exhausted lane."""
    def gone():
        o = env.load_state().out.get(lane)
        if o and o.set >= since and o.until > env.now() and o.reason in ("limit", "credit", "logged_out"):
            return o.why or "lane marked out"
        return None
    return gone


def secret_in(e: Any):
    """The command a Cmd or CmdEnd event names a secret path in, else None (delegate polices the stream of the CLIs that have no hook)."""
    return e.text if secret_hit(e.text) else None


def denied_items(hx: Harness, read_only: bool | None, engine_only: tuple[str, ...] = ()):
    """The `denied` items of a run, from the denials its harness reported. A git write the coordinator forbids, or a write on a read-only step, is
    delegate's policy, not the owner's call, and Claude's own record of it is dropped."""
    out = []
    for x in hx.evidence.denials:
        cmd = (x.input or {}).get("command")
        if hx.kind == "claude" and policy.is_policy_denial(x.tool, x.input, bool(read_only), engine_only):
            continue
        kind = x.kind if x.kind != "policy" else "secret" if hx.kind == "opencode" and x.tool == "Bash" and secret_hit(cmd) else "policy"
        out.append(policy.denial_item(policy.denial(kind, x.tool, x.input)))
    return out


def prepare_codex(d: dict, brief: str, a: env.Args, rid: str, t: float, cfg: dict):
    h = d["how"]
    hold = HoldBook(env.load_state().holds).covering(d.get("lane") or "OPENAI_SUBSCRIPTION", d.get("role", getattr(a, "role", None)), t)
    if hold:
        return HoldBook.refusal(hold, "no worker was launched")
    exe = shutil.which("codex")
    if not exe:
        return {"outcome": "setup", "say": "codex CLI not found on PATH"}
    hx = CodexHarness(a.resume)
    spec = hx.launch_spec(LaunchInputs(
        exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], sandbox=h["sandbox"], out_file=str(env.config_dir() / f"codex-last-{rid[:8]}.txt"), env=decide.codex_env(env.child_env(), h["cwd"], h["sandbox"]),
        cli=h["cli"], access=decide.CODEX_ACCESS, isolation=h["isolation"], schema=a.schema, resume=a.resume,
        prompt=" ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, ESCALATE_RULE, DENIED_RULE)) + "\n\n" + brief))
    before = (codex_account.thread_total(a.resume) or env.history_store().last_cumulative(a.resume)) if a.resume else None
    return Prep(hx, spec, {"before": before, "started": time.time()})       # wall clock, not env.now(): it is compared with file times


def prepare_claude(d: dict, brief: str, a: env.Args, rid: str, t: float, cfg: dict):
    """claude -p on the logged-in subscription (CLAUDE_SUBSCRIPTION, unattended)."""
    h = d["how"]
    exe = shutil.which("claude")
    if not exe:
        return {"outcome": "setup", "say": "claude CLI not found on PATH"}
    hx = ClaudeHarness()
    spec = hx.launch_spec(LaunchInputs(
        exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], prompt=brief, env=env.child_env(), schema=a.schema, resume=a.resume, session=rid,
        system=PREAMBLE + repo_instruction_text(h["cwd"]), settings=json.dumps(guard_settings(h.get("read_only"), tuple(getattr(a, "engine_only", None) or ()))),
        write=bool(h["write"])))
    return Prep(hx, spec, {})


def prepare_opencode(d: dict, brief: str, a: env.Args, rid: str, t: float, cfg: dict):
    """OpenCode headless runner for subscription and isolated free-only lanes."""
    h, lane = d["how"], d["lane"]
    exe = OpenCodeHarness.find_exe()
    if not exe or not OpenCodeHarness.supported():
        return {"outcome": "setup", "say": f"{lane} needs macOS and the opencode CLI (route.py doctor)"}
    nested = opencode_sandbox.preflight()
    if nested:        # before any model token: the lane is out for a while, so the next call routes to the next lane
        settler().mark_out(lane, t + 3600, f"nested_sandbox: {nested}", t, "limit", "guess")
        return {"outcome": "setup", "say": f"nested_sandbox: {lane} commands cannot run here ({nested}); this process is already inside a sandbox. "
                                           "Rerun to use the next lane."}
    if lane == "ZAI_CODING_SUBSCRIPTION" and h["model"] not in harnesses.ZAI_MODELS:       # pin subscription routing; never substitute the pay-as-you-go `zai` provider
        return {"outcome": "setup", "say": "Z.ai Coding Plan model must use the subscription provider."}
    rules = " ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, opencode_sandbox.RULE, DENIED_RULE))
    if not any((x / "AGENTS.md").is_file() for x in decide.repo_dirs(h["cwd"])):
        rules += "\n\n" + repo_instruction_text(h["cwd"])         # opencode reads AGENTS.md itself; CLAUDE.md only through what is turned off
    if a.schema:
        rules += "\n\nYour final message must be only a JSON object matching this JSON Schema:\n" + Path(a.schema).read_text(encoding="utf-8")
    hx = OpenCodeHarness(harnesses.OC_PROFILES.get(lane, Profile()), env.config_dir())
    spec = hx.launch_spec(LaunchInputs(exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], prompt=rules + "\n\n" + brief, env=env.child_env(),
                                       resume=a.resume, session=rid, write=bool(h["write"])))
    if lane == "DEEPSEEK_FREE" and not hx.free_model_ok(h["model"], spec.env):        # fail closed if the exact free model or its zero price is gone
        hx.cleanup()
        until = t + 900
        settler().mark_out(lane, until, "exact free model unavailable or zero catalog price unverified", t, "limit", "guess")
        return {"outcome": "rate_limited", "retry_after": until,
                "say": "Free DeepSeek is unavailable or its zero price could not be verified; rerun to use the next configured lane."}
    return Prep(hx, spec, {})


PREPARE = {"codex": prepare_codex, "claude_p": prepare_claude, "opencode": prepare_opencode}
REF_FIELD = {"codex": "thread", "claude_p": "session", "opencode": "session"}        # the agent-runs field the vendor's session id goes in
AGENT_KIND = {"codex": "codex", "claude_p": "claude-p", "opencode": "opencode"}


def login_text(kind: str, lane: str):
    """(the hint stored with a lane that is logged out, the text of the answer with {why})."""
    if kind == "codex":
        return "logged out: run `codex login`", "codex is not logged in: {why} (route.py doctor)"
    if kind == "claude_p":
        return "logged out: run `claude auth login`", "claude is not logged in: {why} (claude auth login)"
    hint = ("free OpenCode access refused; no paid credentials will be used" if lane == "DEEPSEEK_FREE" else "run `opencode auth login` and choose Z.AI Coding Plan; enter your Coding Plan key in your own terminal"
            if lane == "ZAI_CODING_SUBSCRIPTION" else "run `opencode auth login` (xAI, SuperGrok Subscription)")
    return hint, f"{lane}: {hint}: {{why}}"


def codex_usage(res: dict, x: dict):
    """Codex's usage is a thread's running total: keep this run's part, add the reviewer's tokens (its own child thread), and read the context size."""
    tokens = accounting.per_call(res["tokens"], x["before"])
    if res["ref"]:
        extra, n = codex_account.child_tokens(res["ref"], x["started"] - 1)
        if n:
            base = tokens or {"in": 0, "cached": 0, "out": 0}
            tokens = {k: (base.get(k) or 0) + extra[k] for k in extra}
    res.update(tokens=tokens, context_k=codex_account.context_k(res["ref"]), acct=accounting.codex(tokens))


def lane_cleared(lane: str) -> Callable[[StateDoc], Any]:
    """The state change that drops the out-mark of `lane`."""
    def clear(s: StateDoc) -> Any:
        return s.out.pop(lane, None)
    return clear


def launch_worker(d: dict, brief: str, a: env.Args, rid: str, t: float, cfg: dict, meta: dict, hooks: RunHooks):
    """One worker, end to end: its harness builds the launch, the supervisor runs it, the one ladder settles it. -> dict of result fields."""
    h = d["how"]
    prep = PREPARE[h["kind"]](d, brief, a, rid, t, cfg)
    if isinstance(prep, dict):
        return prep
    hx, spec, x = prep
    field = REF_FIELD[h["kind"]]

    def on_ref(ref: str):
        if hooks.ref:
            hooks.ref(ref)
        agent_runs.run(rid, **{field: ref})
    if hx.evidence.ref:
        on_ref(hx.evidence.ref)        # known before the first event: a resume, or claude's own --session-id
    agent_runs.run(rid, kind=AGENT_KIND[h["kind"]], lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running",
                   **({field: hx.evidence.ref} if hx.evidence.ref else {}), **meta)
    live = LiveSink(hx.kind, run_id=rid)
    live.start(lane=d.get("lane"), model=d.get("model"), effort=d.get("effort"), role=d.get("role") or getattr(a, "role", None))
    log = LogWatcher(hx.log_dir, classify.LIMIT_RE.search, hx.echoes_input) if hx.log_dir else None
    sup = Supervisor(hx, watch.CLI_OF_KIND[h["kind"]], dataclasses.replace(hooks, ref=on_ref), live, gone=lane_gone_check(d["lane"], time.time()), log=log,
                     policy=secret_in if hx.secret_event else None, net_up=lambda: net.net_up(d["lane"]))
    try:
        out = sup.run(spec, a.timeout)
    finally:
        hx.cleanup()
    hx.export(spec.env)
    with contextlib.suppress(Exception):        # one history row per inter-event gap over watch.LONG_GAP_S (role "gap"): the health fold ignores it
        sup.dog.flush(env.history_add, d["lane"], d["model"], out.watch.why is None)
    sm, w = out.summary, out.watch
    res: dict[str, Any] = dict(sm.res, stderr=out.err, denied=denied_items(hx, h.get("read_only"), tuple(getattr(a, "engine_only", None) or ())))
    if h["kind"] == "codex":
        codex_usage(res, x)
    if out.stream_tail:
        res["stream_tail"] = out.stream_tail
    if w.why == "hung":
        res["hung_words"], res["hung_kind"] = w.words, w.kind
    facts = dict(sm.facts, watchdog=w.why, msg=w.msg, secret=out.secret)
    o = classify.classify(sm.rc, sm.vendor, sm.stderr, out.timed_out, sm.work, facts) if sm.check else None
    if out.stop == "answer" and o and o.reason == "killed" and sm.stands and not out.timed_out:       # we stopped it after its answer: the answer stands
        o = None
        res["warnings"] = ["answer_done_process_alive"]
    if out.net and (o is None or o.reason == "killed"):       # we stopped it because the network is down
        o = classify.Outcome("network", hx.evidence.transient, "event")
    if o:       # the CLI's own error or stderr, never the model's answer
        settler().apply(res, o, d, t, hx.kind, a, sm.rc, sm.tail, reset=(lambda: codex_reset_now(t)) if hx.kind == "codex" else None,
               login=login_text(h["kind"], d["lane"]))
    else:
        env.update_state(lane_cleared(d["lane"]))
        res.update(data=sm.data if sm.data is not None else answer.parse_data(sm.text, a.schema))
        if sm.warnings:
            res["warnings"] = sm.warnings
    return res
