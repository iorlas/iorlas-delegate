# iorlas-delegate

A Claude Code plugin that hands sub-tasks to worker models through a deterministic router. The model (or a script) calls `route.py run ROLE` with a brief; the script picks the lane, model and effort, runs the worker and prints one JSON result; it reads plan limits and reset times, applies the fallback chains, and records state. One skill, `delegate`, with modes that are read only when needed.

## Install (any machine)

```sh
claude plugin marketplace add iorlas/iorlas-marketplace
claude plugin install iorlas-delegate@iorlas-marketplace
```

Then, in a Claude Code session, run `/iorlas-delegate:init`. It checks the machine, installs the Codex CLI and plugin, starts the Codex device login (you open a link and enter a code), and tells you the one command to store the API key yourself. A configured machine answers `READY` and reads nothing more.

Requirements: Claude Code. Base mode (Claude sub-agents only) needs nothing else. The router, Codex lane and credit lane need Python 3.8+ (standard library only); the Codex CLI needs Node 18.18+. macOS, Linux, Windows, Docker. In a container, mount `~/.codex` and `~/.config/delegate` as volumes.

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
skills/delegate/route.py      the router: run, next, status, report, capture-limits, doctor, set-key, config
skills/delegate/onboard.py    readiness checks and setup steps
skills/delegate/agent_runs.py publishes runs and lane limits as agent-runs v1 (a public contract)
skills/delegate/routing.json  lanes, models, roles, thresholds
commands/                     /iorlas-delegate:init, /iorlas-delegate:status
tests/                        python3 -m unittest discover -s tests   (CI: 3 OS x Python 3.8 and 3.12)
```

Git stays with the coordinator: workers never create branches or worktrees and never commit, merge or push; `run` compares HEAD and branch before and after and reports a change in `git_violation`.

Dashboards: each Codex and `claude -p` run and the lanes' limits are published as [agent-runs v1](https://github.com/iorlas/iorlas-task-loop/blob/main/docs/contracts/agent-runs-v1.md) records in `~/.config/agent-runs` (`skills/delegate/agent_runs.py` is the only writer). That format is a public contract; everything else here is internal and may change.

State, ledger, settings, profile and the key file live in Claude Code's data folder for the plugin (`~/.claude/plugins/data/iorlas-delegate-<marketplace>`), owner-only. A checkout has no default and refuses to run until `DELEGATE_CONFIG_DIR` names a folder, so state is never split between two places. Uninstalling the plugin deletes the folder, key included. Nothing secret is in this repository. Work is tracked with beads (`bd list`).
