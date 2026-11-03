---
name: delegate
description: Delegate a task to another model or subscription, only when the user or a skill asks to delegate. route.py picks lane, model and effort; never pick them yourself.
---

`DELEGATE_DEPTH` set = you are a worker: do not delegate.
Use Bash `run_in_background`.
`python3 ${CLAUDE_SKILL_DIR}/route.py run ROLE [--cwd D] [--authored-by F] [--engine-only RECIPE]... [--only-family F]... < brief`
`--engine-only-advisory`: disclose in brief.
`--schema`: re-ask/reroute. `--task`: avoid timeout/schema failures or warn.
Roles: planner (plan) · worker (implement) · reviewer (`--authored-by`: author's family) · light (read).

| answer | do |
|---|---|
| `stop` null, `agent_model` | Agent call: `agent_model`, `brief_prefix` + brief, in `cwd`, no worktree, effort in brief |
| `stop` null | `text` is the answer; rerun acceptance check. `git_violation`: show it, check `git reflog`, never auto-revert. |
| `ask_user` | ask; yes = `--allow-degraded` |
| `stop.retry.kind` predicted/poll | rerun at `stop.retry.at`; busy includes `--only-family` |
| `stop.reason` timeout | shrink the task |
| other `stop` | tell user `say`, `stop.fix`; setup: `modes/init.md`; no secret in chat |
| `needs_input` `claude_usage` | `get_usage`; rerun with `--claude-usage '<plan.windows>'` |
| `notice` | tell the user once |

No Python 3.8+: Agent tool only.

<!-- base-table -->
| role | agent, in order |
|---|---|
| planner | sonnet high |
| worker | sonnet medium |
| reviewer | sonnet medium |
| light | haiku low > sonnet low |
<!-- /base-table -->

Review never blocks: only the author's family up = self-review (`flag` same_family: say so).

Brief: goal, files, constraints, acceptance, "no commits". Own git; parallel writers: separate directories. `--premium` only if user names it. Never delegate scope, design, plan, acceptance.
`modes/`: init codex extend zai-coding deepseek-free
