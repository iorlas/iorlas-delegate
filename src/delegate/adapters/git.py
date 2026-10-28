"""The only code that runs git for delegate: the repo root of a folder, and the HEAD and branch a worker must leave alone."""
import os
import shutil
import subprocess
from pathlib import Path


def _git(cwd: object, *args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def repo_root(cwd: str | os.PathLike[str]) -> Path:
    """The work tree's top folder, or cwd resolved when it is not in one."""
    wd = Path(cwd).resolve()
    top = (_git(wd, "rev-parse", "--show-toplevel") or "").strip() if shutil.which("git") else ""
    return Path(top).resolve() if top else wd


def state(cwd: object) -> dict | None:
    """HEAD and current branch of cwd, or None when cwd is not a git work tree. Other refs and worktrees change while parallel slots work and land, so they are not compared."""
    if shutil.which("git") is None or _git(cwd, "rev-parse", "--is-inside-work-tree") is None:
        return None
    return {"head": (_git(cwd, "rev-parse", "HEAD") or "").strip(), "branch": (_git(cwd, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()}


def changes(before: dict | None, after: dict | None) -> list[str]:
    """-> human-readable differences; empty when the worker left HEAD and branch alone."""
    if before is None or after is None:
        return []
    out = []
    if before["branch"] != after["branch"]:
        out.append(f"branch {before['branch'] or '(detached)'} -> {after['branch'] or '(detached)'}")
    if before["head"] != after["head"]:
        out.append(f"HEAD {before['head'][:9]} -> {after['head'][:9]}")
    return out
