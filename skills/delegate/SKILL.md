---
name: delegate
description: Strategy for handing sub-tasks to worker models. Defines three execution lanes (Codex, Claude Max, Claude API credit), a fallback chain per role (planner, worker, reviewer, hard, bulk) that names every lane, a rule for choosing between Max and the credit from live limits, and when to stop. Use before delegating any planning, implementation, debugging, review, research or bulk-reading sub-task from an orchestrating session. Mechanics live in the lane skills.
---

# delegate

The orchestrating session (Opus on Max) decides; workers execute bounded sub-tasks. Every role has a chain of fallbacks that names all three lanes and ends in STOP. Take the first step whose lane is not out and whose gate passes. Read the state first, keep the model floor, tell the user in one line when the state changes the route.

If the environment variable `DELEGATE_DEPTH` is set, you are a worker: do not delegate further.

## Lanes

| lane | pays with | models | how to run | resets |
|---|---|---|---|---|
| `codex` | ChatGPT Plus subscription | GPT-6.1 Sol, GPT-6 Luna | skill `codex-worker` | rolling 5-hour window plus a weekly cap |
| `max` | Claude Max subscription, shared with the orchestrator and Fable | Opus 5.5, Sonnet 5.5, Haiku 5.5 | native Agent tool, `model:` set per call | weekly moment fixed per account (`MAX_WEEKLY_RESET`, the user's is Sunday 17:00 local) plus a rolling 5-hour window |
| `credit` | the Max plan's monthly API credit (prepaid) | Opus, Sonnet, Haiku (Fable only on request) | skill `claude-credit-worker` | billing cycle (`CREDIT_RESET_DAY`, the 1st); no balance reading |

`max` and `credit` are the two Claude sources. Allowance that is unused and expires soonest is free; allowance that resets last must be rationed.

## Never delegated

Decisions stay with the orchestrator: requirements and scope, design and architecture, game-design judgement, the plan and its split into sub-tasks, whether a result is accepted, what becomes a new ticket. A worker may be asked for options or findings; the choice is the orchestrator's.

## Read the state before every sub-task

| lane | reading |
|---|---|
| `max` | Desktop app Code tab: `mcp__ccd_session_mgmt__get_usage` (deferred: load it with ToolSearch once): 5-hour limit, weekly limit for all models, weekly limit for Fable, each with percent used and reset time. Elsewhere `limits.py` reads the status-line file; failing that it knows only the reset moment |
| `codex` | `python3 ${CLAUDE_SKILL_DIR}/limits.py`: live 5-hour and weekly use, reset times |
| `credit` | no balance. `limits.py` shows the reset date, this script's own spend against its cap, and the out marker `run.sh` writes on "credit balance too low" |

Pace = share of the window elapsed minus share used. Positive means slack; negative means spending faster than the window allows. `limits.py` prints verdicts OK / BURN / OUT / STALE / UNKNOWN. BURN: the weekly window resets within 2 hours with 10% or more left, so that allowance is about to be lost.

## Max or credit: choosing the Claude source

Written `claude(S)` below. Default is `max`; spend a little credit when Max is strained; return to Max when it recovers. The first matching row wins.

| condition | S |
|---|---|
| `max` BURN, or a reading with the weekly reset within 2 hours | `max` |
| `credit` cycle ends within 3 days and this script's spend is under 70% of its cap | `credit` (use it before it resets) |
| `max` 5-hour ≥ 80%, or weekly pace ≤ -15, or weekly ≥ 90% | `credit` |
| `max` 5-hour < 50% and weekly pace > -5 | `max` |
| between those thresholds | keep the previous source |
| `credit` OUT (exit 75 or out marker, exit 77 cap) | `max` |
| `max` OUT, or weekly ≥ 90% (the orchestrator needs the rest) | `credit` |
| both OUT | no Claude source: go on to the chain's next step |

`claude(other)` is the other source, tried next. Never push `max` weekly past 90% with native subagents: the orchestrator stops when Max stops.

## Roles and fallback chains

Gate for `codex` steps: verdict not OUT and 5-hour < 90%. If `codex` pace ≤ -15 while the Claude source has pace ≥ +25, swap the two steps so the Claude source goes first. Efforts below are floors; do not go lower without saying so. Profiles are `claude-credit-worker` profiles; on `max` use the same model and effort in the Agent call.

**Planner** (brainstorm, plan, design, split a ticket). No silent degrading: a bad plan costs more than waiting.

| step | lane | model, effort |
|---|---|---|
| 1 | claude(S) | Opus 5.5 medium (`max`: the orchestrator itself or Agent `model: opus`; `credit`: `--profile planner`) |
| 2 | claude(other) | Opus 5.5 medium |
| 3 | `codex` | Sol high, read-only: `/codex:adversarial-review` on the draft plan, or `codex-rescue` read-only with a planning brief |
| 4 | STOP | tell the user there is no adequate reasoning model now; give each lane's reset time. Offer the degraded options and wait: Opus 5.5 low (preferred, it is cheaper per task than Sonnet in the benchmark), or Sonnet 5.5 high |

**Worker** (implement, debug, tests).

| step | lane | model, effort |
|---|---|---|
| 1 | `codex` | Sol medium |
| 2 | claude(S) | Sonnet 5.5 medium (`--profile worker`) |
| 3 | claude(other) | Sonnet 5.5 medium |
| 4 | STOP | hold the task, report the earliest reset; a few lines the orchestrator may write itself |

**Reviewer** (verify, review a diff). The reviewer comes from a different family than the writer. Always rerun the acceptance check; the reviewer reports exit codes and the last lines of output.

| step | writer was Codex | writer was Claude |
|---|---|---|
| 1 | claude(S) Opus 5.5 medium (`--profile reviewer`) | `codex` Sol high read-only: `/codex:review` |
| 2 | claude(other) Opus 5.5 medium | claude(S) Opus 5.5 medium |
| 3 | claude(S or other) Opus 5.5 low, flagged degraded | claude(other) Opus 5.5 medium |
| 4 | STOP: mark the diff unreviewed; the orchestrator reads it only if small or risk-flagged | Opus 5.5 low flagged degraded, then STOP as on the left |

Never Sol reviewing Sol. Risk flags that make the orchestrator read the diff itself: public API change, test weakened or deleted, suppression added, files outside the brief.

**Hard** (two workers failed it, or it needs real design reasoning inside the code). Split into short subtasks, check each, and let only the failed subtask climb, one rung at a time:

| rung | lane | model, effort |
|---|---|---|
| 1 | `codex` | Sol high |
| 2 | claude(S), then claude(other) | Opus 5.5 low (`--profile hard`) |
| 3 | claude(S), then claude(other) | Opus 5.5 medium (`--profile very-hard`) |
| 4 | STOP | report what was tried; Fable is available only if the user says so |

If a rung needs more than a couple of tries, the plan or the split is wrong; rethink that before climbing.

**Bulk** (read a codebase, summarise logs, classify, small clear edits).

| step | lane | model, effort |
|---|---|---|
| 1 | `codex` | GPT-6 Luna, read-only (`codex exec -m gpt-6-luna`): very large Plus allowance, weak model; reading and summarising only |
| 2 | claude(S) | Haiku 5.5 low (`--profile cheap`); small clear edits: Sonnet 5.5 low (`--profile light`) |
| 3 | claude(other) | same |
| 4 | STOP or do it directly if tiny |

## Fable

Only when the user's task text names Fable. Never escalate to it on your own. Run it as `claude-credit-worker --profile impossible` (Fable 5.1 medium, $6 cap) or Agent `model: fable` on `max`, where it counts against the weekly Fable cap (half of the weekly limit).

## Rules

1. **Out, then back.** A lane is out when its skill or reading says so. Note its reset time. Before a new sub-task, a lane whose reset time has passed (or, when unknown, an hour after it went out) gets one cheap probe from its skill; if it answers, use it again.
2. **Brief like a ticket:** goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests". Workers do not see this session; name the project rules the task needs.
3. **Never trust a worker's summary.** The verifier reruns the acceptance check from the brief, not from the writer's report.
4. **Failed fix:** resume the same worker thread with the findings. Two failed fixes: rethink the plan; only then raise effort or climb.
5. **Parallel writers need separate git worktrees.** Parallel sessions share each lane's allowance: expect `codex` to run out faster and the chains to move on.
6. **Cheap read-only questions** (one grep, one short file) need no worker.
7. **The project's runbook wins** on how a worker runs there (sandbox limits, build flags, forbidden commands): read it before the first brief, e.g. a repo's `docs/agents/codex.md`.
8. Missing or broken lane skill, key or login: say so in one line and take the next step; do not improvise another route.
