"""The decision a call starts with: build the fleet, route, remember the lane choice (`decision`, shared by `next` and `run`), and what a chosen
step runs on (`how`). The codex access flags and the repo's instruction files, which both lanes get the same, are here too."""
import json
import os
import random
import sys
from pathlib import Path

from delegate.adapters import agent_runs, git
from delegate.app import env, onboard
from delegate.app.fleet import FleetBuilder, drift_warnings, mark_busy, settle_holds
from delegate.app.usage import READERS
from delegate.contracts.routing import CallArgs, Run
from delegate.contracts.stores import CheckpointStore
from delegate.domain.holds.book import HoldBook
from delegate.domain.routing.router import Router, callable_lanes

# Every codex run and resume gets these, so neither the user's config nor a repo file widens or narrows them. The sandbox has no network and
# writes only the working directory; a command it blocks may ask to run outside it, and Codex's automatic reviewer (a model, no human)
# approves or refuses that one command. Codex permission profiles would also block reads (~/.ssh), but an approved escalation there
# stays inside the sandbox, so Unity cannot run (codex-cli 0.162.1).
CODEX_ACCESS = ["--ignore-user-config", "--ignore-rules", "-c", "sandbox_workspace_write.network_access=false",
                "-c", "sandbox_workspace_write.writable_roots=[]", "-c", "approval_policy=on-request", "-c", "approvals_reviewer=auto_review"]


def repo_dirs(cwd):
    """The folders from the repo root (or cwd outside a repo) down to cwd."""
    wd = Path(cwd).resolve()
    root = git.repo_root(wd)
    out = [wd]
    while out[-1] != root and out[-1].parent != out[-1] and root in out[-1].parents:
        out.append(out[-1].parent)
    return out[::-1]


def repo_instructions(cwd):
    """The repo's instruction files, the same for both lanes (Codex's own rule): in each folder from the root down to cwd, AGENTS.md, else CLAUDE.md."""
    found = []
    for d in repo_dirs(cwd):
        f = next((d / n for n in ("AGENTS.md", "CLAUDE.md") if (d / n).is_file()), None)
        if f:
            found.append(f)
    return found


def codex_isolation(cwd):
    """Codex sees only what delegate sends plus the repo's instruction files: every skill it would list (its own system skills, the user's,
    the repo's .agents/skills) is turned off by path, and a repo with only CLAUDE.md is read as Claude would. Sub-agents cannot be turned off
    (codex 0.162.1: features.multi_agent=false and agents.max_depth=0 still let a brief that asks spawn one); NO_SUBAGENTS_RULE asks, and their
    tokens are counted as child threads."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    roots = [home / "skills", Path.home() / ".agents" / "skills"] + [d / x / "skills" for d in repo_dirs(cwd) for x in (".agents", ".codex")]
    paths = sorted({str(f.resolve()) for r in roots if r.is_dir() for f in r.rglob("SKILL.md")})
    return ["-c", "skills.config=[" + ",".join("{path=%s,enabled=false}" % json.dumps(x) for x in paths) + "]",
            "-c", 'project_doc_fallback_filenames=["CLAUDE.md"]']


def how(lane, step, cfg, cwd=None, unattended=False):
    """What a step runs on. Unattended callers have no Agent tool, so the subscription lane runs `claude -p` on the logged-in account."""
    m = cfg["models"][step["model"]]
    runner = cfg["lanes"][lane.name]["runner"]
    wd = os.path.abspath(cwd) if cwd else os.getcwd()
    write = step.get("mode") == "write"
    if runner == "agent_tool" and unattended:
        runner = "claude_p"
    if runner == "claude_p":
        return {"kind": "claude_p", "model": m["api"], "write": write, "cap": step.get("cap", 2), "cwd": wd, "read_only": not write}
    if runner == "agent_tool":
        return {"kind": "agent_tool", "model": m["agent"], "cwd": wd}
    if runner == "opencode":
        return {"kind": "opencode", "model": m["api"], "write": write, "cwd": wd}
    sandbox = "workspace-write" if write else "read-only"
    iso = codex_isolation(wd)
    cli = ["codex", "exec", "-C", wd, "-s", sandbox, "-m", m["api"], "-c", f"model_reasoning_effort={step['effort']}"] + CODEX_ACCESS + iso
    if git.state(wd) is None:
        cli.append("--skip-git-repo-check")      # codex refuses a directory that is not a git work tree
    return {"kind": "codex", "model": m["api"], "cli": cli, "cwd": wd, "sandbox": sandbox, "isolation": iso}


def parse_json_arg(v):
    if not v:
        return None
    if v == "-":
        v = sys.stdin.read()
    elif v.startswith("@"):
        v = Path(v[1:]).read_text(encoding="utf-8")
    try:
        return json.loads(v)
    except ValueError as e:
        sys.exit(f"route.py: --claude-usage is not valid JSON: {e}")


def busy_jitter():
    return random.randint(60, 120)


def fleet_builder(cfg):
    return FleetBuilder(env.typed_routing(cfg), env.states(), env.history_store(), CheckpointStore(env.config_dir() / "health-checkpoint.json"), env.config_store(),
                        READERS, lambda: onboard.configured_lanes(cfg))


def call_args(a, unattended):
    return CallArgs(authored_by=a.authored_by, premium=a.premium,
                    allow_degraded=a.allow_degraded, unattended=unattended, resume=getattr(a, "resume", None), cap=getattr(a, "cap", None))


def decision(a, cfg, unattended=False):
    """next and run share this: build the fleet, route, remember the lane choice. -> (d, lanes, warnings, t); d is the JSON answer of `next`."""
    t = env.now()
    st = env.load_state()
    routing = env.typed_routing(cfg)
    fleet = fleet_builder(cfg).build(st, t, parse_json_arg(a.claude_usage))
    warnings = drift_warnings(env.states(), fleet.lanes, st, t)
    _, st = settle_holds(env.states(), fleet.lanes, st, t)
    publish_lanes(fleet.lanes, st, t)
    lanes = callable_lanes(mark_busy(fleet.lanes, st, routing.settings, t, busy_jitter), unattended)
    args = call_args(a, unattended)
    router = Router(routing, HoldBook(st.holds), st.prev, t)
    pick = router.route(a.role, args, lanes, st.refs.get(args.resume or ""))
    d = pick.as_dict()
    if isinstance(pick, Run):
        step = {"model": pick.model, "effort": pick.effort, "mode": pick.mode}
        if pick.cap is not None or pick.pass_ == "resume":
            step["cap"] = pick.cap
        d["how"] = how(lanes[pick.lane], step, cfg, a.cwd, unattended)
        if pick.pass_ != "resume":
            fam = lanes[pick.lane].family
            if args.cap:
                d["cap"] = d["how"]["cap"] = args.cap if d["how"]["kind"] == "claude_p" else d["how"].get("cap")

            def mark(s):
                new = router.dwell.note(fam, pick.lane, t)
                if new:
                    s.prev[fam] = new
            if sum(1 for lane in lanes.values() if lane.family == fam) > 1:
                env.update_state(mark)
    if fleet.offers:
        env.config_store().mark_offered(fleet.offers)
        d["notice"] = "not set up: " + ", ".join(fleet.offers) + " (doctor --lane NAME to set up, doctor --decline NAME to hide)"
    return d, lanes, warnings, t


def publish_lanes(lanes, st, t):
    """Lane limits as agent-runs v1 (agent_runs.py), for dashboards. Never fails the caller."""
    try:
        outs = st.out
        agent_runs.lanes([(n, l.out_until if l.out else None, outs[n].why if l.out and n in outs else None,
                           [("5h", l.five, l.five_reset), ("week", l.week, l.week_reset)]) for n, l in lanes.items()], t)
    except Exception:
        pass
