"""child-env — the environment a child agent process should get.

**Stop copying the env scrub.** A Claude session (desktop app, CLI, plugins) exports ``CLAUDE_*``,
``ANTHROPIC_*``, ``MCP_*``, ``OTEL_*`` and friends to everything it launches; a standalone ``claude`` or
``codex`` started with them misbehaves (nested-session errors, a keychain login that looks expired, a staging
login server). A worker should also not inherit the launcher's tokens and keys. ``scrub(env)`` returns a new
dict without those; ``keep`` rescues names, ``drop`` removes more.

Pure: the input mapping is never mutated and ``os.environ`` is read only by ``scrub_current``.

An entry in ``keep`` / ``drop`` is an exact variable name, or a prefix when it ends with ``*``
(``"MCP_*"``). Precedence: ``drop`` beats everything; ``keep`` beats the defaults.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: Agent-session prefixes: everything a Claude / Codex session exports to its children.
APP_PREFIXES: tuple[str, ...] = ("CLAUDE_", "ANTHROPIC_", "DELEGATE_", "MCP_", "OTEL_", "CODEX_COMPANION")
#: Agent-session variables named exactly (no shared prefix).
APP_NAMES: tuple[str, ...] = (
    "CLAUDECODE",
    "AI_AGENT",
    "API_TIMEOUT_MS",
    "BAGGAGE",
    "DISABLE_MICROCOMPACT",
    "DISABLE_AUTOUPDATER",
    "USE_LOCAL_OAUTH",
    "USE_STAGING_OAUTH",
)
#: Credential variables by prefix (cloud and git-host credentials).
CREDENTIAL_PREFIXES: tuple[str, ...] = ("AWS_", "GH_", "GITHUB_")
#: Credential variables named exactly (the ssh agent socket is a credential: it signs as the user).
CREDENTIAL_NAMES: tuple[str, ...] = ("SSH_AUTH_SOCK",)
#: Credential-looking words anywhere in a name, compared upper-case (``api_key`` and ``APIKEY`` both match).
CREDENTIAL_MARKERS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSW",
    "CREDENTIAL",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "ACCESSKEY",
    "PRIVATE_KEY",
    "PRIVATEKEY",
)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(name.startswith(p[:-1]) if p.endswith("*") else name == p for p in patterns)


def _default_drop(name: str) -> bool:
    upper = name.upper()
    return (
        name.startswith(APP_PREFIXES)
        or name in APP_NAMES
        or upper.startswith(CREDENTIAL_PREFIXES)
        or upper in CREDENTIAL_NAMES
        or any(m in upper for m in CREDENTIAL_MARKERS)
    )


def scrub(env: Mapping[str, str], *, keep: Iterable[str] = (), drop: Iterable[str] = ()) -> dict[str, str]:
    """A new dict: ``env`` without agent-session and credential variables.

    ``keep`` names (or ``PREFIX*``) survive the default rules; ``drop`` names (or ``PREFIX*``) are removed
    even when they would otherwise stay, and win over ``keep``.
    """
    keep_, drop_ = tuple(keep), tuple(drop)
    return {k: v for k, v in env.items() if not _matches(k, drop_) and (_matches(k, keep_) or not _default_drop(k))}


def scrub_current(*, keep: Iterable[str] = (), drop: Iterable[str] = ()) -> dict[str, str]:
    """``scrub(os.environ, ...)``: the only place this package reads the process environment."""
    return scrub(os.environ, keep=keep, drop=drop)


__all__ = [
    "APP_NAMES",
    "APP_PREFIXES",
    "CREDENTIAL_MARKERS",
    "CREDENTIAL_NAMES",
    "CREDENTIAL_PREFIXES",
    "scrub",
    "scrub_current",
]
