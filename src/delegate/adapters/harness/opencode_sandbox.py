"""What keeps an OpenCode worker inside the sandbox: the shell its commands run with (macOS sandbox-exec: no network, no secret paths, writes only
in the work folder and temp) and the permission table OpenCode itself enforces. OpenCode stays outside the sandbox: it needs the network for its
model, and macOS cannot nest one sandbox inside another."""
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from app_dirs import config_dir

SECRETS = (".ssh", ".aws", ".gnupg", ".azure", ".config/gh", ".docker", ".netrc", ".git-credentials", ".codex", ".grok", ".claude", ".claude.json",
           ".local/share/opencode", "Library/Keychains")
RULE = ("You run unattended. Every command runs in a sandbox: no network, no credential files, no writes outside this directory. If a command "
        "fails because of it, do not work around it or change code to avoid it: say which command and why in your final message, and go on without it.")
NETWORK_REFUSALS = ("tcp connect error", "Network is unreachable", "Failed to connect", "error sending request")


def readable_outside() -> list[str]:
    """Folders outside the work folder a read-only worker may read: the caller's runs folder (the engine points reviewers at its files) and temp dirs."""
    return [str(config_dir("task-loop") / "runs"), "/private/tmp", "/private/var/folders", "/tmp", "/var/folders"]


def permissions(write: bool) -> dict:
    reads = {"*": "allow", ".env": "deny", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow", "*.env.sample": "allow", "*.env.template": "allow"}
    for x in SECRETS:
        reads[str(Path.home() / x)] = reads[str(Path.home() / x) + "/*"] = "deny"
    outside = "deny" if write else {"*": "deny", **{x + "/**": "allow" for x in readable_outside()}}
    return {"*": "allow", "read": reads, "webfetch": "deny", "websearch": "deny", "external_directory": outside, "task": "deny", **({} if write else {"edit": "deny"})}


def network_refusal(out: str) -> bool:
    """True when a command's output shows it was refused the network (the shell's sandbox blocks it): that is not an owner decision."""
    return any(x in out for x in NETWORK_REFUSALS)


def _quote(x: object) -> str:
    return json.dumps(os.path.realpath(str(x)))


def write_shell(cwd: str, write: bool, path: Path, roots: Sequence[Path] = ()) -> None:
    """The $SHELL OpenCode runs commands with: each one under sandbox-exec. `roots` are the XDG dirs the launcher points commands at (their tools
    cache and keep state there), writable like temp."""
    home = Path.home()
    secrets = " ".join(f"(subpath {_quote(home / x)})" for x in SECRETS)
    scratch = Path(cwd) / ".task"        # the task-loop engine's git-ignored scratch folder: a reviewer's test run writes its temp files there
    writable = [cwd if write else scratch] + ["/private/tmp", "/private/var/folders", home / "Library" / "Caches", home / ".cache", *roots]
    keep = " ".join(f"(require-not (subpath {_quote(x)}))" for x in writable)
    profile = ('(version 1)(allow default)(deny network-outbound (remote ip))'
               f'(deny file-read* file-write* {secrets} (regex #"/\\.env$") (regex #"/\\.env\\.[^/]*$"))'
               '(allow file-read* (regex #"/\\.env\\.(example|sample|template|dist)$"))'
               f'(deny file-write* (require-all {keep} (require-not (regex #"^/dev/"))))'
               + ("" if write else f"(deny file-write* (require-all (subpath {_quote(cwd)}) (require-not (subpath {_quote(scratch)}))))"))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    head = f"export XDG_CONFIG_HOME={shlex.quote(xdg)}" if xdg else "unset XDG_CONFIG_HOME"      # commands get the user's config root back
    path.write_text(f"#!/bin/sh\n{head}\nexport SHELL=/bin/zsh\nexport UV_OFFLINE=1\nexec /usr/bin/sandbox-exec -p {shlex.quote(profile)} /bin/zsh \"$@\"\n",
                    encoding="utf-8")
    path.chmod(0o700)


PROBE = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"]
_probed: list[str | None] = []


def preflight(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str | None:
    """None when this process can start a sandboxed command, else why not (once per process). macOS cannot nest a sandbox in a sandbox: when
    delegate itself runs inside one (a sandboxed agent session), sandbox-exec exits 71 and every command of the worker would fail."""
    if not _probed:
        try:
            r = run(PROBE, capture_output=True, text=True, timeout=20, check=False)
            _probed.append(None if r.returncode == 0 else f"sandbox-exec exited {r.returncode}: {(r.stderr or '').strip()[:160]}")
        except (OSError, subprocess.SubprocessError) as e:
            _probed.append(f"sandbox-exec could not run: {e}")
    return _probed[0]
