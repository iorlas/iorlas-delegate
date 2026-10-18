---
name: delegate
description: Delegate a sub-task to a worker model. route.py picks lane, model and effort from plan limits; never pick them yourself. Use before delegating, and for setup.
---

`DELEGATE_DEPTH` set = you are a worker: do not delegate.
`python3 ${CLAUDE_SKILL_DIR}/route.py run ROLE [--cwd D] [--writer openai|claude] [--failed I,J] [--premium] [--allow-degraded] [--claude-usage JSON] < brief`
(`py -3` on Windows.) Roles: planner (plan, design) · worker (implement, debug) · reviewer (needs `--writer`) · hard (after 2 failures; `--failed`) · light (read, summarise, tiny edits).

| `status` | do |
|---|---|
| `agent` | Agent call: `agent_model`, `brief_prefix` + brief, in `cwd`, no worktree, effort in the brief |
| `done` | `text` is the answer; rerun the acceptance check. `git_violation`: show it, check `git reflog`, never auto-revert |
| `stop` | tell the user `say`. `ask_user`: ask; yes = `--allow-degraded` |
| `lane_out` | rerun; it advances |
| `timeout` | shrink the task |
| `failed`, `error` | read `say`; setup: `modes/init.md`; never ask for a key in chat |
| `needs_input` `claude_usage` | `get_usage`; rerun with `--claude-usage '<plan.windows>'` |
| `notice` | tell the user once |

No Python 3.8+: Agent tool only, first entry, no worktree, effort in the brief.

<!-- base-table -->
| role | agent, in order |
|---|---|
| planner | opus medium |
| worker | sonnet medium |
| reviewer | opus medium |
| hard | opus low > opus medium |
| light | haiku low > sonnet low |
<!-- /base-table -->

Brief: goal, files, constraints, acceptance check, "no commits". You own git; parallel writers: one directory each. `--premium` only if the user names that model. Never delegate scope, design, plan, acceptance.
`modes/`: init, codex, claude-credit, extend.
