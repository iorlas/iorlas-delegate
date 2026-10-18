---
name: delegate
description: Hand a sub-task (plan, implement, debug, review, read) to a worker model. route.py picks the lane, model and effort from plan limits; never choose them yourself. Use before delegating any sub-task, and for setup.
---

# delegate

`route.py` decides. Never pick a lane, model or effort. `DELEGATE_DEPTH` set = you are a worker: do not delegate.
Run `python3 ${CLAUDE_SKILL_DIR}/route.py` (`py -3` on Windows). Python 3.8+, no packages.

## Do

```
route.py run ROLE [--cwd DIR] [--writer openai|claude] [--failed IDX,IDX] [--premium] [--allow-degraded] [--claude-usage JSON] < brief
```

| result | do |
|---|---|
| `action:"agent"` | Agent call: `agent_model`, brief prefixed with `brief_prefix`, in `cwd`, no worktree, effort stated in the brief |
| exit 0, `done` | stdout is the worker's answer. Rerun the brief's acceptance check; trust nothing else |
| `action:"stop"` | tell the user `say` and `resets`, one line each. `ask_user` set: ask it; yes = rerun with `--allow-degraded` |
| exit 76 | lane is now out. Rerun the same command |
| exit 78 | worker changed git. Show the line, inspect `git reflog`, decide. Never auto-revert |
| exit 124 | worker timed out and was killed. Shrink the task |
| exit 2 or `error` | read `${CLAUDE_SKILL_DIR}/modes/init.md`. Never ask for a key in chat |
| `needs_input` has `claude_usage` | call `get_usage` (load via ToolSearch once), rerun with `--claude-usage` |
| `warnings` | tell the user once. `pass:"relaxed"`: one line |

Long jobs: Bash `run_in_background`. `route.py next ROLE` shows the choice without running it.

## Roles

| role | use |
|---|---|
| planner | brainstorm, plan, design, split a ticket |
| worker | implement, debug, tests |
| reviewer | review a diff; needs `--writer` |
| hard | two workers failed; `--failed` = indexes that failed |
| light | read many files, summarise logs, classify, tiny edits |

`--premium`: only when the user names the premium model.

## Base mode (no Python 3.8+, or route.py will not start)

Agent tool only, in the current directory, no worktree. State the effort in the brief. First entry; no limit checks.

<!-- base-table -->
| role | agent model + effort, in order |
|---|---|
| planner | opus medium |
| worker | sonnet medium |
| reviewer | opus medium |
| hard | opus low > opus medium |
| light | haiku low > sonnet low |
<!-- /base-table -->

Codex and credit lanes need Python. Tell the user once.

## Rules

1. Brief like a ticket: goal, files, constraints, acceptance check, "no commits, no pushing, no weakened tests". Workers do not see this session.
2. You own git: branches, worktrees, commits, merges. Parallel writers: one directory each, made by you, as `--cwd`.
3. Failed fix: resume the same worker with the findings. Two failures: rethink, then `hard`.
4. Never delegate: scope, design, the plan, acceptance, new tickets.
5. A one-grep question needs no worker.

Modes (read only when needed): `modes/init.md` setup, `modes/codex.md` Codex quirks, `modes/claude-credit.md` credit lane and exit codes, `modes/extend.md` add a model, role or lane.
