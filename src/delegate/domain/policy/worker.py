"""The policy a worker runs under (D3): the rules told to it, the typed record of a refused call, and the verdict on one tool call."""
from dataclasses import dataclass
from typing import Any

from delegate.contracts import events as ev
from delegate.domain.policy.matchers import heredoc_only_hit, secret_hit
from delegate.domain.policy.shell import READ_ONLY_HELP, WRITER_RULE, git_write, read_only_problem, writer_problem

EDIT_RULE = "Edit files with the patch/edit tool (apply_patch), never with shell heredocs or `python -c` file rewrites."
APPLY_PATCH_HINT = "rewrite this edit with apply_patch (the secret name is only inside a heredoc body; heredoc and `python -c` rewrites are blocked)"
SECRET_RULE = "Never touch credential files (~/.ssh, .env, ~/.aws, tokens, keychains): it is blocked. If the task needs one, stop and say so."
GIT_RULES = ("Git is the coordinator's. Stay in the current directory. Do not create or switch branches or worktrees; "
             "do not commit, amend, merge, rebase, reset, stash, tag or push. Read-only git is fine. Leave changes uncommitted.")
CHECKED_FIELDS = ("command", "file_path", "path", "pattern", "glob")


@dataclass(frozen=True)
class Verdict:
    """The answer to one tool call: `blocked` with the words the worker reads, or allowed."""

    blocked: bool = False
    reason: str = ""


def judge(tool_name: str | None, tool_input: dict[str, Any], read_only: bool, engine_only: tuple[str, ...] = ()) -> Verdict:
    """A tool call that names a secret path, changes git, or (on a read-only step) does more than read is blocked. Only the fields that name
    what is run or opened are checked, not file contents being written (a .gitignore listing .env is fine)."""
    hit = secret_hit(" ".join(str(tool_input.get(k) or "") for k in CHECKED_FIELDS))
    if hit:
        hint = f" {APPLY_PATCH_HINT}." if heredoc_only_hit(str(tool_input.get("command") or "")) else ""
        return Verdict(True, f"blocked by delegate: {hit} is a secret path.{hint} {SECRET_RULE}")
    cmd = str(tool_input.get("command") or "") if tool_name == "Bash" else ""
    gw = git_write(cmd)
    if gw:
        return Verdict(True, f"blocked by delegate: `{gw}` changes git, which is the coordinator's. {GIT_RULES}")
    why = read_only and cmd and read_only_problem(cmd)
    if why:
        return Verdict(True, f"blocked by delegate: {why}; {READ_ONLY_HELP}")
    why = not read_only and cmd and writer_problem(cmd, engine_only)
    if why:
        return Verdict(True, f"blocked by delegate: {why}; {WRITER_RULE}")
    return Verdict()


def denial(kind: str, tool: str, inp: Any) -> ev.Denied:
    """One typed refusal. kind: `policy` (a reviewer or classifier refused the call: the owner decides), `secret` (the call named a secret path),
    `sandbox_limit` (the sandbox blocked a call that needs a system resource, like ps: noise, never a failure)."""
    return ev.Denied(tool=tool, input=inp if isinstance(inp, dict) else {}, kind=kind)


def denied_path(d: ev.Denied) -> str | None:
    """The secret the denied call matched, else the call's own path argument."""
    inp = d.input or {}
    return secret_hit(inp.get("command")) if d.kind == "secret" else inp.get("file_path") or inp.get("filePath") or inp.get("path")


def denial_item(d: ev.Denied) -> dict[str, Any]:
    """The `denied` item of a run's answer."""
    return {"kind": d.kind, "tool": d.tool, "path": denied_path(d), "input": d.input}


def is_policy_denial(tool_name: str | None, tool_input: dict[str, Any] | None, read_only: bool = False, engine_only: tuple[str, ...] = ()) -> bool:
    """A git write the coordinator forbids, or a write on a read-only step, is delegate's policy, not a decision for the owner (a secret path stays one)."""
    cmd = str((tool_input or {}).get("command") or "").strip()
    return tool_name == "Bash" and not secret_hit(cmd) and bool(git_write(cmd) or (read_only and read_only_problem(cmd)) or (not read_only and writer_problem(cmd, engine_only)))
