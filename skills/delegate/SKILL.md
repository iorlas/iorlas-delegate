---
name: delegate
description: Delegate a sub-task to another model or subscription, only when the user or a skill asks to delegate. route.py picks lane, model and effort; never pick them yourself.
---

`DELEGATE_DEPTH` set = you are a worker: do not delegate.
Run it with Bash `run_in_background`.
`python3 ${CLAUDE_SKILL_DIR}/route.py run ROLE [--cwd D] [--authored-by openai|claude] [--failed I,J] [--premium] [--allow-degraded] [--claude-usage JSON] < brief`
(`py -3` on Windows.) Roles: planner (plan, design) · worker (implement, debug) · reviewer (`--authored-by`: family that wrote it) · hard (after 2 failures; `--failed`) · light (read, summarise).

| `status` | do |
|---|---|
| `agent` | Agent call: `agent_model`, `brief_prefix` + brief, in `cwd`, no worktree, effort in the brief |
| `done` | `text` is the answer; rerun the acceptance check. `git_violation`: show it, check `git reflog`, never auto-revert. `flag` same_family: say so |
| `stop` | tell the user `say`. `ask_user`: ask; yes = `--allow-degraded` |
| `lane_out` | rerun; it advances |
| `timeout` | shrink the task |
| `failed`, `error` | read `say`; setup: `modes/init.md`; no key in chat |
| `needs_input` `claude_usage` | `get_usage`; rerun with `--claude-usage '<plan.windows>'` |
| `notice` | tell the user once |

No Python 3.8+: Agent tool only, no worktree, effort in the brief.

<!-- base-table -->
| role | agent, in order |
|---|---|
| planner | opus medium |
| worker | sonnet medium |
| reviewer | opus medium |
| hard | opus low > opus medium |
| light | haiku low > sonnet low |
<!-- /base-table -->

Brief: goal, files, constraints, acceptance check, "no commits". You own git; parallel writers: one directory each. `--premium` only if the user names it. Never delegate scope, design, plan, acceptance.
`modes/`: init codex claude-credit extend
