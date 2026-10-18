---
name: delegate
description: Strategy for handing sub-tasks to worker models: which worker first, when to fall to the next, when to come back, and how to treat hard tasks. Use before delegating any implementation, debugging, research, verification or review sub-task from an orchestrating session. Mechanics live in the lane skills.
---

# delegate

The orchestrating session (Opus) decides; workers execute bounded sub-tasks. Spend the lanes in order, fall to the next
only when the current one is out, and come back up as soon as a higher lane resets. Each lane skill holds its own
commands, limits and exhaustion signal; load it before using the lane.

| lane | worker | skill | draws on | resets |
|---|---|---|---|---|
| 1 | Codex, GPT-6.1 Sol | `codex-worker` | ChatGPT Plus allowance | the 5-hour window (or the weekly cap); the limit message names the time |
| 2 | Claude (Sonnet, Haiku, Opus) via `claude -p` on an API key | `claude-credit-worker` | the $200/mo API credit; never Max | the billing cycle (exit 75) |
| 3 | Sonnet via the native Agent tool, `model: sonnet` | (built in) | Max limits, shared with the orchestrator and Fable | last resort |

If the environment variable `DELEGATE_DEPTH` is set, you are a worker: do not delegate further.

## Never delegated

Decisions stay with the orchestrator: requirements and scope, design and architecture, game-design judgement, the plan
and its split into sub-tasks, whether a result is accepted, what becomes a new ticket. A worker may be asked for
options or findings; the choice is the orchestrator's.

## Pacing: read the Max limits before every sub-task

In the Claude desktop app's Code tab, `mcp__ccd_session_mgmt__get_usage` (a deferred tool: load it with ToolSearch once)
returns the Max windows: the 5-hour limit, the weekly limit for all models, the weekly limit for Fable, each with the
percent used and the reset time. Elsewhere (the terminal CLI) there is no such reading: follow the lane order as
written. Codex and the API credit have no reading; their skills detect "out" by the error.

| reading | do |
|---|---|
| 5-hour ≥ 80%, or weekly ≥ 90% | Max is for the orchestrator only: no lane 3, no Fable, no native subagents; workers on lanes 1-2. If both are out, tell the user once and do the work in the session only when it is small |
| the weekly resets within 2 h with more than 30% unused, and the 5-hour is under 60% | the unused weekly is about to be lost: move lane 3 above lane 2 (native Sonnet before the credit) until it resets; lane 1 stays first |
| otherwise | the lane order as written |

The 5-hour window caps every reading: unused weekly cannot be spent faster than the 5-hour window allows, and an
orchestrator that hits it stops the whole loop. Tell the user in one line when pacing changes the order.

## Check the limits first

Before each new sub-task run `python3 ${CLAUDE_SKILL_DIR}/limits.py` (one line per lane: use, reset time, verdict).

- **BURN**: the weekly window resets within two hours and 10% or more is left. That allowance is lost at the reset, so spend it first, ahead of the usual order. Typical case: Max weekly resets in 30 minutes, so send work to lane 3 now.
- **OUT**: skip the lane until its reset time. This replaces guessing from error text; the error-text probes in the lane skills remain the fallback.
- **STALE / UNKNOWN**: no fresh reading; use the usual order and the lane skill's own signals.
- The credit lane has no reading; it is OUT only when `run.sh` exits 75.
- `limits.py` reads Max from `~/.claude/state/max-limits.json`, which the status line writes (`capture-limits.sh`). Without that wiring Max stays UNKNOWN.

## Rules

1. **Out, then back.** A lane is out when its skill says so. Note its reset time (the limit message, or "unknown").
   Before every new sub-task, check: a higher lane whose reset time has passed (or, when unknown, an hour since it ran
   out) gets one cheap probe from its skill; it answers → use it again. Tell the user once, in one line, when a lane
   runs out and when it comes back.
2. **What goes where.** Implementation, debugging, boilerplate, tests → lane 1 first. Verification (rerun the checks,
   read the diff, run what lane 1's sandbox cannot) → lane 2 first: a Codex worker must not judge its own work, and
   lane 2 runs unsandboxed. Bulk reading (search a codebase, summarise long logs) → lane 2 with Haiku or Sonnet.
3. **Brief like a ticket:** goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests".
   Workers do not see this session; lane 2 skips CLAUDE.md, so name the project rules the task needs.
4. **Never trust a worker's summary.** The verifier reruns the acceptance check from the brief, not from the writer's
   report, and returns raw exit codes and the last lines of output. The orchestrator reads that short report; it reads
   the diff itself only on a risk flag (public API change, test weakened or deleted, suppression added, files outside
   the brief) or for a hard sub-task.
5. **Failed fix:** resume the same worker thread with the findings. Two failed fixes: rethink the plan; only then raise
   effort or switch lane.
6. **Parallel writers need separate git worktrees.** Parallel sessions share each lane's allowance: lane 1 runs out
   faster with several sessions, so expect the fall to lane 2 and the climb back.
7. **Second opinion from another model family:** the review commands in `codex-worker`.
8. **Cheap read-only questions** (one grep, one short file) need no worker; do them directly.
9. **The project's runbook wins** on how a worker runs there (sandbox limits, build flags, commands it must not run):
   read it before the first brief, e.g. a repo's `docs/agents/codex.md`.

## Hard tasks: Opus, short and split

Complexity, not length, is the trigger: a task two workers have failed, or one that needs real design reasoning inside
the code.

1. Split it into subtasks, each small enough to finish in one short Opus run and to check on its own.
2. Run each subtask separately at low effort, and check each result before starting the next. Never give Opus the whole
   ticket in one run.
3. Run Opus through lane 2 first (`claude-credit-worker --profile hard`: Opus 5.5, low effort, $3 cap). On Max, only if the credit is gone.
4. Raise effort above low only for a subtask that failed at low.

## Notes

- The Max weekly limit is shared with the orchestrator and with Fable. Spend it last.
- Missing or broken lane skill, key or login: say so in one line and fall to the next lane; do not improvise a different
  route.
