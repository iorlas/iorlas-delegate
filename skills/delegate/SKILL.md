---
name: delegate
description: Hand a planning, implementation, debugging, review or reading sub-task to a worker model by asking the deterministic router script (route.py) which lane, model and effort to use, then executing its answer. The script reads plan limits and reset times and applies the fallback chains; the model never improvises the route. One skill with modes (init, codex, claude-credit, extend). Use before delegating any sub-task from an orchestrating session, and for setup (init).
---

# delegate

`route.py` decides. You supply what it cannot read, execute its answer, report refusals. Never pick a lane, model or effort yourself. If the environment variable `DELEGATE_DEPTH` is set you are a worker: do not delegate.
Run: `python3 ${CLAUDE_SKILL_DIR}/route.py ...` (`python` or `py -3` on Windows). Python 3.8+, standard library only. Data: `routing.json`. User settings and state: `~/.config/delegate` (`%APPDATA%\delegate`).

| mode | when | read |
|---|---|---|
| run | any sub-task (default) | this file |
| init | `next` returns `setup_required`; the user runs `/iorlas-delegate:init` | `${CLAUDE_SKILL_DIR}/modes/init.md` |
| codex | executing a step on OPENAI_SUBSCRIPTION | `modes/codex.md` |
| claude-credit | executing a step on CLAUDE_CREDIT; exit codes; key | `modes/claude-credit.md` |
| extend | adding a model, role or lane | `modes/extend.md` |
| status | the user asks where limits stand | run `route.py status` |

Read a mode file only when you need it.

## Loop

```
d = route.py next ROLE [--cwd DIR] [--writer openai|claude] [--failed IDX,IDX] [--premium [FAMILY]] [--allow-degraded] [--claude-usage JSON]

switch d.action:
  setup_required: read modes/init.md and run it; then call next again
  run:    prefix the brief with d.how.brief_prefix; run d.how.guard.before; execute d.how (table below); run d.how.guard.after
          relay d.warnings once; mention d.pass if "relaxed" (one line)
          guard.after or run-claude exit 78 = the worker changed git state (see Git below)
          if the lane refused for a limit or credit reason:
              route.py report --lane d.lane --event out --message "<error text>"; goto next
          else judge the result (rules below)
  stop:   tell the user d.say and d.resets, one line each. nothing else.
          if d.ask_user: ask it; on yes call next again with --allow-degraded
  error:  fix the call (missing --writer, unknown role)
for item in d.needs_input: do item.how, then call next again
```

Roles (chains live in `routing.json`): `planner` (brainstorm, plan, design, split a ticket), `worker` (implement, debug, tests), `reviewer` (verify or review a diff; needs `--writer` = the family that wrote it), `hard` (two workers failed it; pass `--failed` with the indexes of steps that ran and failed; only the failed subtask climbs), `light` (read many files, summarise logs, classify, small mechanical edits; not about parallelism).

## You supply

| input | when | how |
|---|---|---|
| Claude subscription use | each call in the Claude desktop app | tool `mcp__ccd_session_mgmt__get_usage` (deferred: load via ToolSearch once); pass `--claude-usage '{"five_hour":{"used":N,"resets_at":T},"weekly":{"used":N,"resets_at":T}}'`. Elsewhere the script reads the status-line file |
| writer family | role `reviewer` | `--writer openai` or `--writer claude` |
| steps that failed their acceptance check | role `hard` | `--failed 0,1` |
| the user asked for the premium model by name (today: Fable, family claude) | only then | `--premium` (or `--premium claude`); never add it yourself. A family with no premium model mapped returns an error |
| the user said yes to a degraded option | after a stop with `ask_user` | `--allow-degraded` |

## Execute d.how

| d.how.kind | do |
|---|---|
| `command` (CLAUDE_CREDIT) | run `argv`, brief on stdin; Bash `run_in_background` if long. Exit codes: modes/claude-credit.md |
| `agent_tool` (CLAUDE_SUBSCRIPTION) | an Agent call with `model` as given, in the current directory: never `isolation: worktree`; state the effort in the brief |
| `codex` (OPENAI_SUBSCRIPTION) | modes/codex.md, by `d.use` |

`d.pass == "relaxed"`: the strict gates (the reserve kept for the orchestrator, the per-lane gates) left nothing, so a held-back allowance is used for the same step, up to a hard ceiling. Nothing about the step changes.

## Git

The coordinator (this session, or the loop above it such as task-loop) owns branches, worktrees, commits, merges and pushes. A worker runs in the directory it is given (`--cwd`; default: the current one), leaves its changes uncommitted, and never creates a branch or worktree. Workers get this rule in `d.how.brief_prefix`; the Claude runner also denies every git write command. `guard before/after` (run-claude does it itself) compares branch, HEAD, refs and worktrees: a difference exits 78 with `GIT-VIOLATION`. The file changes stay on disk; inspect with `git reflog` and decide, never auto-revert.

## Never delegated

Requirements and scope, design and architecture, game-design judgement, the plan and its split into sub-tasks, whether a result is accepted, what becomes a new ticket. A worker may be asked for options or findings; the choice is yours.

## Rules the script cannot enforce

1. Brief like a ticket: goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests". Workers do not see this session; name the project rules the task needs.
2. Never trust a worker's summary: rerun the acceptance check from the brief. Read the diff yourself on a risk flag: public API change, test weakened or deleted, suppression added, files outside the brief.
3. Failed fix: resume the same worker thread with the findings. Two failed fixes: rethink the plan, then role `hard`.
4. Parallel writers each get their own directory (a worktree the coordinator made) as `--cwd`; two writers in one directory collide. Parallel sessions share each lane's allowance.
5. A cheap read-only question (one grep, one short file) needs no worker.
6. The project's runbook wins on how a worker runs there (sandbox limits, build flags, forbidden commands): read it before the first brief.
7. Tell the user in one line when a lane goes out, comes back, or the relaxed pass is used.
