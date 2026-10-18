---
name: delegate
description: Routing rules for handing sub-tasks to worker models. Three execution lanes (CODEX, MAX, CREDIT), a function that picks between the two Claude lanes from live limits, and an ordered chain per role (planner, worker, reviewer, hard, light) that ends in STOP. Use before delegating any planning, implementation, debugging, review, or reading sub-task from an orchestrating session. Commands live in the lane skills.
---

# delegate

Treat this file as a program: glossary, one function, one table of chains, a few rules. Do not improvise outside it.
If the environment variable `DELEGATE_DEPTH` is set you are a worker: do not delegate.

## Glossary

| term | meaning |
|---|---|
| lane | a place work runs, named by what pays for it |
| `CODEX` | lane. GPT models through the Codex plugin. Pays with the ChatGPT Plus subscription. Resets: rolling 5-hour window plus a weekly cap. Skill: `codex-worker` |
| `MAX` | lane. Claude models through the native Agent tool (`model:` per call). Pays with the Claude Max subscription, which the orchestrator itself also uses. Resets: a rolling 5-hour window plus a weekly moment (`MAX_WEEKLY_RESET`, the user's is Sunday 17:00 local) |
| `CREDIT` | lane. Claude models through `claude -p` with an API key. Pays with the Max plan's monthly API credit (prepaid). Resets on `CREDIT_RESET_DAY` (the 1st). No balance reading. Skill: `claude-credit-worker` |
| Claude lane | `MAX` or `CREDIT`: the two lanes that run the same Claude models |
| `CLAUDE_PREFERRED` | the Claude lane that `pick_claude_lane()` returns now |
| `CLAUDE_OTHER` | the other Claude lane |
| role | the kind of job: planner, worker, reviewer, hard, light |
| step | one entry of a role's chain: a model, an effort, and the lanes it may run on |
| effort | reasoning level of a model: low, medium, high |
| fallback | go to the next step because no lane of this step is available |
| escalation | go to the next step because this step's result failed the acceptance check |
| STOP | end of a chain: do not run anything; report to the user |
| out | a lane cannot be used now (limit reached, credit empty, cap reached) until its reset time |
| pace | share of a window already elapsed minus share used, in points. Positive: slack. Negative: spending faster than the window allows |

## Never delegated

Requirements and scope, design and architecture, game-design judgement, the plan and its split into sub-tasks, whether a result is accepted, what becomes a new ticket. A worker may be asked for options or findings; the choice is the orchestrator's.

## Read the state before every sub-task

| lane | where to read it |
|---|---|
| `MAX` | Desktop app Code tab: tool `mcp__ccd_session_mgmt__get_usage` (deferred: load it with ToolSearch once): 5-hour limit, weekly limit for all models, weekly limit for Fable, each with percent used and reset time. Elsewhere: `python3 ${CLAUDE_SKILL_DIR}/limits.py` (status-line file; failing that only the reset moment) |
| `CODEX` | `python3 ${CLAUDE_SKILL_DIR}/limits.py`: 5-hour and weekly use with reset times |
| `CREDIT` | no balance. `limits.py` shows the reset date, the spend this script logged against its cap, and the out marker that `run.sh` writes on "credit balance too low" (exit 75; exit 77 = own cap) |

`limits.py` prints OK / BURN / OUT / STALE / UNKNOWN per lane. BURN: the weekly window resets within 2 hours and 10% or more is left, so that allowance is about to be lost.

## Function: pick_claude_lane

```
def pick_claude_lane(max, credit, previous):
    # hard limits first
    if max.out and credit.out:                   return NONE
    if credit.out:                               return MAX if (max.weekly_used < 90 and max.five_hour_used < 80) else NONE
    if max.out or max.weekly_used >= 90:         return CREDIT      # the orchestrator needs what is left of Max
    # use-it-or-lose-it
    if max.burn:                                 return MAX
    if credit.resets_within_days(3) and credit.own_spend < 0.7 * credit.cap:  return CREDIT
    # spend a little credit when Max is strained, come back when it recovers
    if max.five_hour_used >= 80 or max.weekly_pace <= -15:   return CREDIT
    if max.five_hour_used < 50 and max.weekly_pace > -5:     return MAX
    return previous or MAX                                  # between thresholds: keep the last choice
```

`CLAUDE_OTHER` = the other of MAX / CREDIT, if it is not out.

## Chains

```
def available(lane):  return not lane.out and (lane != CODEX or codex.five_hour_used < 90)

def run_role(role, task):
    steps = ROLES[role].steps
    if role == worker and codex.pace <= -15 and claude.pace >= 25: swap(steps[0], steps[1])
    for step in steps:
        if step is STOP: report(step.say); return
        lane = first(l for l in step.lanes if available(l))
        if lane is None: continue                           # fallback
        result = run(lane, step.model, step.effort, task)
        if ROLES[role].advance == "on_failure" and failed(result): continue   # escalation
        return result
```

Keep the model floor: never run a step below its listed effort without saying so. Commands: `CODEX` via `codex-worker`; `CREDIT` via `claude-credit-worker --profile <profile>`; `MAX` via the Agent tool with the same `model`.

```yaml
planner:            # brainstorm, plan, design, split a ticket. Never degrade silently.
  advance: on_unavailable
  steps:
    - {model: opus-5.5, effort: medium, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}   # CREDIT profile: planner; on MAX the orchestrator itself may do it
    - {model: gpt-6.1-sol, effort: high, read_only: true, lanes: [CODEX]}          # /codex:adversarial-review on the draft plan
    - STOP: "no adequate reasoning model now. Give each lane's reset time. Offer: opus-5.5 low (preferred: cheaper per task than Sonnet), or sonnet-5.5 high. Run a degraded option only after the user says yes."

worker:             # implement, debug, write tests
  advance: on_unavailable
  steps:
    - {model: gpt-6.1-sol, effort: medium, lanes: [CODEX]}
    - {model: sonnet-5.5, effort: medium, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}   # CREDIT profile: worker
    - STOP: "hold the task; report the earliest reset. A few lines the orchestrator may write itself."

reviewer:           # verify, review a diff. The reviewer is from a different model family than the writer. Always rerun the acceptance check; report exit codes and the last lines of output.
  advance: on_unavailable
  steps_if_writer_is_codex:
    - {model: opus-5.5, effort: medium, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}   # CREDIT profile: reviewer
    - {model: opus-5.5, effort: low, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER], flag: degraded}
    - STOP: "mark the diff unreviewed; the orchestrator reads it only if small or risk-flagged"
  steps_if_writer_is_claude:
    - {model: gpt-6.1-sol, effort: high, read_only: true, lanes: [CODEX]}          # /codex:review
    - {model: opus-5.5, effort: medium, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}
    - {model: opus-5.5, effort: low, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER], flag: degraded}
    - STOP: "mark the diff unreviewed"
  never: "gpt-6.1-sol reviewing a diff written by gpt-6.1-sol"
  risk_flags: [public API change, test weakened or deleted, suppression added, files outside the brief]   # the orchestrator reads the diff itself on any of these

hard:               # two workers failed it, or it needs real design reasoning inside the code. Split into short subtasks; only the failed subtask climbs.
  advance: on_failure          # each step runs only if the previous one failed the acceptance check
  steps:
    - {model: gpt-6.1-sol, effort: high, lanes: [CODEX]}
    - {model: opus-5.5, effort: low, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}      # CREDIT profile: hard
    - {model: opus-5.5, effort: medium, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}   # CREDIT profile: very-hard
    - STOP: "report what was tried. If a step needed more than a couple of tries, the plan or the split is wrong: rethink it. Fable only if the user says so."

light:              # simple, low-risk, low-reasoning work: read many files, summarise logs, classify, mechanical small edits. Not about parallelism.
  advance: on_unavailable
  steps:
    - {model: gpt-6-luna, effort: low, read_only: true, lanes: [CODEX]}            # huge Plus allowance, weak model: reading and summarising only, never edits or judgement
    - {model: haiku-5.5, effort: low, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}     # CREDIT profile: cheap
    - {model: sonnet-5.5, effort: low, lanes: [CLAUDE_PREFERRED, CLAUDE_OTHER]}    # small clear edits; CREDIT profile: light
    - STOP: "or do it directly if it is tiny"
```

Why two steps can show the same model and effort with different lanes: that is one step. The model is fixed; the lane is whichever of the listed lanes is available.

## Fable

Only when the user's task text names Fable. Never choose it yourself. `CREDIT`: `--profile impossible` (Fable 5.1, medium, $6 cap). `MAX`: Agent `model: fable`, which counts against the weekly Fable cap (half of the weekly limit).

## Rules

1. **Out, then back.** When a lane is out, note its reset time. Before a new sub-task, a lane whose reset time has passed (or, if unknown, an hour after it went out) gets one cheap probe from its skill; if it answers it is available again. Tell the user in one line when a lane goes out, comes back, or changes the route.
2. **Brief like a ticket:** goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests". Workers do not see this session; name the project rules the task needs.
3. **Never trust a worker's summary.** The verifier reruns the acceptance check from the brief, not from the writer's report.
4. **Failed fix:** resume the same worker thread with the findings. Two failed fixes: rethink the plan; only then change effort or use the `hard` chain.
5. **Parallel writers need separate git worktrees.** Parallel sessions share each lane's allowance: expect `CODEX` to run out sooner.
6. **Cheap read-only questions** (one grep, one short file) need no worker.
7. **The project's runbook wins** on how a worker runs there (sandbox limits, build flags, forbidden commands): read it before the first brief, e.g. a repo's `docs/agents/codex.md`.
8. Missing or broken lane skill, key or login: say so in one line and take the next step; do not improvise another route.
