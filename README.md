# iorlas-delegate

A Claude Code plugin that hands sub-tasks to worker models through a deterministic router. The model asks `route.py next ROLE` and gets back which lane, model and effort to run; the script reads plan limits and reset times, applies the fallback chains, and records state. One skill, `delegate`, with modes that are read only when needed.

## Install (any machine)

```sh
claude plugin marketplace add iorlas/iorlas-marketplace
claude plugin install iorlas-delegate@iorlas-marketplace
```

Then, in a Claude Code session, run `/iorlas-delegate:init`. It checks the machine, installs the Codex CLI and plugin, starts the Codex device login (you open a link and enter a code), and tells you the one command to store the API key yourself. A configured machine answers `READY` and reads nothing more.

Requirements: Python 3.8+, Claude Code, Node 18.18+ (Codex CLI). macOS, Linux, Windows, Docker. In a container, mount `~/.codex` and `~/.config/delegate` as volumes.

## Lanes

| lane | pays with | credential |
|---|---|---|
| `CLAUDE_SUBSCRIPTION` | the Claude Max plan, shared with the orchestrator | none |
| `CLAUDE_CREDIT` | the Max plan's monthly API credit | an API key, stored by you with `route.py set-key` |
| `OPENAI_SUBSCRIPTION` | the ChatGPT Plus plan, through Codex | `codex login` |

Roles: `planner`, `worker`, `reviewer`, `hard`, `light`. Chains, models and thresholds are data in `skills/delegate/routing.json`. The premium tier (Fable for claude) runs only when the user asks for it by name (`--premium`).

## Layout

```
skills/delegate/SKILL.md      dispatcher and the run loop (the only file read on a normal call)
skills/delegate/modes/        init, codex, claude-credit, extend: read on demand
skills/delegate/route.py      the router: next, report, status, run-claude, capture-limits, doctor, init, set-key, config
skills/delegate/onboard.py    readiness checks and setup steps
skills/delegate/routing.json  lanes, models, roles, thresholds
commands/                     /iorlas-delegate:init, /iorlas-delegate:status
tests/                        python3 -m unittest discover -s tests   (CI: 3 OS x Python 3.8 and 3.12)
```

Git stays with the coordinator: workers never create branches or worktrees and never commit, merge or push; a before/after guard exits 78 if one does.

State, ledger, settings and the key file live in `~/.config/delegate` (`%APPDATA%\delegate`), owner-only. Nothing secret is in this repository. Work is tracked with beads (`bd list`).
