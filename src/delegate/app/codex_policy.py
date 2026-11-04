"""Guard I/O and per-call Codex host grants. Rules allow only the guard shim; it validates the whole argv before dispatch."""
import dataclasses
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from jsonl_log import JsonlLog

from delegate.adapters.harness.base import LaunchSpec
from delegate.adapters.harness.codex_overlay import CodexOverlay
from delegate.app import env
from delegate.contracts import stops, wire
from delegate.domain.policy.exec_allow import allowed_argv, call_rules, host_policy
from delegate.domain.policy.worker import Verdict, judge


class ExecAllowError(ValueError):
    """A per-call host grant cannot be honored by the selected step."""


def require_writer_mode(how: dict, value: object) -> None:
    if value != [] and (how["kind"] != "codex" or how.get("sandbox") != "workspace-write"):
        raise ExecAllowError("writer_exec_allow requires a Codex write step")


def admit_request(value: object, engine_only: tuple[str, ...] = ()) -> None:
    if value == []:
        return
    wire.require_writer_exec_allow(stops.CONTRACT)
    binaries, host = host_policy(json.loads((env.config_dir() / "host.json").read_text(encoding="utf-8")))
    call_rules(value, binaries, host, engine_only)


def configure_writer(spec: LaunchSpec, value: object, state: Path,
                     engine_only: tuple[str, ...] = ()) -> tuple[LaunchSpec, CodexOverlay | None]:
    if value == []:
        return spec, None
    state = state.resolve()
    binaries, host = host_policy(json.loads((state / "host.json").read_text(encoding="utf-8")))
    rules = call_rules(value, binaries, host, engine_only)
    real_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
    shim = [sys.executable, str(env.SKILL_DIR / "route.py"), "guard", "--config-dir", str(state)]
    shim += [f"--engine-only={recipe}" for recipe in engine_only]
    overlay = CodexOverlay(state, real_home, shim, rules)
    try:
        argv = ["approval_policy=never" if x.startswith("approval_policy=") else x for x in spec.argv if x != "--ignore-rules"]
        argv += ["-c", "sqlite_home=" + json.dumps(str(real_home))]
        # Dropping --ignore-rules must not admit repository rule files, even under an inherited trust decision.
        cwd = Path(spec.cwd).resolve()
        for folder in (cwd, *cwd.parents):
            argv += ["-c", f'projects.{json.dumps(str(folder))}.trust_level="untrusted"']
        prompt = ("\n\nHost execution is restricted to these guarded commands (append arguments only to the permitted test/build forms):\n"
                  + "\n".join(shlex.join([*overlay.prefix, *rule]) for rule in rules)
                  + "\nUse the guard invocation exactly. Other commands remain sandboxed; no escalation or prompt is available.")
        return dataclasses.replace(spec, argv=argv, env=spec.env | {"CODEX_HOME": str(overlay.root)}, stdin=spec.stdin + prompt), overlay
    except BaseException:
        overlay.cleanup()
        raise


def hook_verdict(payload: object, read_only: bool, engine_only: tuple[str, ...]) -> Verdict:
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str) or not payload["tool_name"]:
        raise ValueError("invalid hook tool_name")
    inp = payload.get("tool_input")
    if not isinstance(inp, dict):
        raise ValueError("invalid hook tool_input")
    if payload["tool_name"] == "Bash" and (not isinstance(inp.get("command"), str) or not inp["command"].strip()):
        raise ValueError("invalid Bash command")
    return judge(payload["tool_name"], inp, read_only, engine_only)


def guard(a: env.Args) -> int:
    """Persist one authorization decision before dispatch; storage errors never start an executable."""
    argv = getattr(a, "exec_allow", None)
    read_only = bool(getattr(a, "read_only", False))
    engine_only = tuple(getattr(a, "engine_only", None) or ())
    row: dict = {"v": 1, "mode": "exec" if argv is not None else "hook", "argv": argv,
                 "allowed": False, "reason": "guard error"}
    result = 2
    resolved = None
    try:
        root = Path(a.config_dir) if getattr(a, "config_dir", None) else env.config_dir()
        with JsonlLog.open(root / "guard.jsonl") as audit:
            try:
                row.update(ts=env.now(), cwd=os.getcwd())
                binaries, rules = host_policy(json.loads((root / "host.json").read_text(encoding="utf-8")))
                if argv is None:
                    payload = json.load(sys.stdin)
                    verdict = hook_verdict(payload, read_only, engine_only)
                    row.update(tool=payload["tool_name"], command=payload["tool_input"].get("command"))
                    row.update(allowed=not verdict.blocked, reason=verdict.reason)
                    result = 2 if verdict.blocked else 0
                else:
                    if getattr(a, "exec_policy", None):
                        rules = call_rules(json.loads(Path(a.exec_policy).read_text(encoding="utf-8")), binaries, rules, engine_only)
                    resolved = allowed_argv(argv, binaries, rules, read_only, engine_only)
                    row["resolved_argv"] = resolved
                    row.update(allowed=True, reason="")
                    result = 0
            except (Exception, SystemExit) as error:
                row.update(allowed=False, reason=f"guard refused: {type(error).__name__}: {error}")
            if not audit.append_json(row):
                raise OSError("audit append refused")
        if row["allowed"] and resolved is not None:
            result = subprocess.run(resolved, check=False).returncode
    except (Exception, SystemExit) as error:
        print(f"delegate guard: config/audit failure: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if not row["allowed"]:
        print(row["reason"], file=sys.stderr)
    return result
