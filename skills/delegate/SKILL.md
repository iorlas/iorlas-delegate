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

## Read the state before every sub-task

| lane | reading |
|---|---|
| Max | Desktop app Code tab: `mcp__ccd_session_mgmt__get_usage` (deferred: load it with ToolSearch once) returns the 5-hour limit, the weekly limit for all models and the weekly limit for Fable, each with percent used and reset time. Elsewhere: `limits.py` reads the status-line file; failing that it knows only the weekly reset moment from config |
| Codex | `python3 ${CLAUDE_SKILL_DIR}/limits.py`: live 5-hour and weekly use with reset times |
| Credit | no balance reading. `limits.py` shows the cycle's reset date, this script's own spend against its cap, and whether `run.sh` saw "credit balance too low" |

`limits.py` prints one block per lane (use, reset, verdict OK / BURN / OUT / STALE / UNKNOWN, pace) and a suggested `order:`.
Pace is the share of the window that has elapsed minus the share used: positive means slack, negative means spending faster than the window allows.

**The three lanes reset on different clocks:** Codex on a rolling 5-hour window plus a weekly cap; Max on a weekly moment fixed per account (the user's is Sunday 17:00 local, kept in `MAX_WEEKLY_RESET`) plus a rolling 5-hour window; the credit on the billing cycle (`CREDIT_RESET_DAY`, the 1st), which rarely lines up with the Max week. Allowance that is unused and expires soonest is free. Allowance that resets last must be rationed.

| state | do |
|---|---|
| a lane is BURN: its weekly window resets within 2 h, 10% or more is left (for Max also a 5-hour reading under 60%) | spend it first, ahead of the written order; that allowance is lost at the reset |
| Max 5-hour ≥ 80%, or Max weekly ≥ 90% | Max is for the orchestrator only: no lane 3, no Fable, no native subagents; workers on lanes 1-2. If both are out, tell the user once and do the work in the session only when it is small |
| a lane's pace ≤ -15 while another lane's pace ≥ +25 | send routine work to the lane with slack; keep the strained lane for what it does best |
| credit is OUT (exit 75, or the out marker) | skip it until its reset date; do not probe it earlier |
| a lane is OUT or STALE or UNKNOWN | skip OUT; for STALE / UNKNOWN use the written order and the lane skill's own error signals |
| otherwise | the lane order as written |

The 5-hour window caps every reading: unused weekly cannot be spent faster than the 5-hour window allows, and an orchestrator that hits it stops the whole loop. Tell the user in one line when the state changes the order.

## Rules

1. **Out, then back.** A lane is out when its skill says so. Note its reset time (the limit message, or "unknown").
   Before every new sub-task, check: a higher lane whose reset time has passed (or, when unknown, an hour since it ran
   out) gets one cheap probe from its skill; it answers → use it again. Tell the user once, in one line, when a lane
   runs out and when it comes back.
2. **What goes where.** Implementation, debugging, boilerplate, tests → lane 1 first. Verification (rerun the checks,
   read the diff, run what lane 1's sandbox cannot) → lane 2 first: a Codex worker must not judge its own work, and
   lane 2 runs unsandboxed. Bulk reading (search a codebase, summarise long logs) → lane 2 with Haiku or Sonnet.
3. **Brief like a ticket:** goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests".
   Workers do not see this session; lane 2 loads the project's CLAUDE.md like a normal run, but still name the rules the task needs.
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

## Hard tasks: climb one rung at a time, on short split subtasks

Complexity, not length, decides. Split the ticket into subtasks, each small enough for one short run and checkable on its own. Check every result before starting the next. Never give a strong model the whole ticket.

| tier | when | worker |
|---|---|---|
| normal | an ordinary task | the lane order |
| hard | two workers failed it, or it needs real design reasoning inside the code | `claude-credit-worker --profile hard`: Opus 5.5, low effort, $3 cap |
| very hard | hard failed on a subtask | `--profile very-hard`: Opus 5.5, medium effort, $5 cap |
| impossible | very-hard failed twice on the same subtask | `--profile impossible`: Fable 5.1, medium effort, $6 cap |

- Only the subtask that failed climbs; the rest of the ticket stays on the cheaper tier.
- These tiers run on lane 2 first. On Max (Agent tool, `model: opus` or `fable`) only if the credit is out; Fable on Max counts against the weekly Fable cap, which is half of the weekly limit.
- Each tier costs several times the one below: if an Opus run needs more than a couple of tries, the plan or the split is wrong; rethink that before climbing.

## Notes

- The Max weekly limit is shared with the orchestrator and with Fable. Spend it last, unless a BURN reading says it is about to be lost.
- Missing or broken lane skill, key or login: say so in one line and fall to the next lane; do not improvise a different
  route.
