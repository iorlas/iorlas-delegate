---
name: delegate
description: Hand a planning, implementation, debugging, review or reading sub-task to a worker model by asking the deterministic router script (route.py) which lane, model and effort to use, then executing what it says. The script reads plan limits and reset times, applies the fallback chains, and never lets the model improvise the route. Use before delegating any sub-task from an orchestrating session.
---

# delegate

`route.py` decides. You provide what it cannot read, execute its answer, and report what happened. Do not pick a lane, model or effort yourself. If the environment variable `DELEGATE_DEPTH` is set you are a worker: do not delegate.

Run it as `python3 ${CLAUDE_SKILL_DIR}/route.py ...` (`python` or `py -3` on Windows). Python 3.8+, standard library only; it works on macOS, Linux, Windows and in Docker. Data (lanes, models, roles, thresholds) is in `routing.json` beside it.

## Loop

```
d = route.py next ROLE [--writer openai|claude] [--failed IDX,IDX] [--fable] [--claude-usage JSON]

if d.action == "run":
    execute d.how                          # see "Executing" below
    if the lane refused for a limit/credit reason:
        route.py report --lane d.lane --event out --message "<the error text>"
        goto next                          # the router now skips that lane
    else: judge the result (rules below)
if d.action == "stop":
    tell the user d.say and d.resets (one line each); do nothing else
    if d.ask_user: ask it; on yes call next again with --allow-degraded
if d.action == "error": fix the call (missing --writer, unknown role)
for each item in d.needs_input: do what item.how says, then call next again
```

Roles: `planner` (brainstorm, plan, design, split a ticket), `worker` (implement, debug, tests), `reviewer` (verify or review a diff; needs `--writer` = the family that wrote it), `hard` (two workers failed it; pass `--failed` with the indexes of steps that ran and failed, and only the failed subtask climbs), `light` (reading many files, summarising logs, classifying, small mechanical edits; not about parallelism). The chains live in `routing.json`.

## What you provide

| input | when | how |
|---|---|---|
| Claude subscription usage | each call in the Claude desktop app | call the deferred tool `mcp__ccd_session_mgmt__get_usage` (load it with ToolSearch once), pass the windows as `--claude-usage '{"five_hour":{"used":N,"resets_at":T},"weekly":{"used":N,"resets_at":T}}'`. Elsewhere the script reads the status-line file; run `route.py capture-limits` from the status line to write it |
| writer family | role `reviewer` | `--writer openai` or `--writer claude` |
| which steps failed their acceptance check | role `hard` | `--failed 0,1` |
| the user named Fable in the task | only then | `--fable`. Never add it yourself |
| the user said yes to a degraded option | after a stop with `ask_user` | `--allow-degraded` |
| a lane refused work | after any refusal | `route.py report --lane L --event out --message "<error text>"` |

## Executing d.how

- `kind: command` (lane CLAUDE_CREDIT): run `argv` with the brief on stdin; for a long task use Bash `run_in_background`. Exit 75 = credit empty (the script already marked the lane out), 76 = rate limited (retry once after a minute), 77 = own cycle cap, 2 = no API key (tell the user the one-time setup it printed). See skill `claude-credit-worker`.
- `kind: agent_tool` (lane CLAUDE_SUBSCRIPTION): an Agent call with `model` as given; effort cannot be set per call, so state it in the brief.
- `kind: codex` (lane OPENAI_SUBSCRIPTION): follow skill `codex-worker` for `use` (task, review, adversarial review of a plan, read-only exec).

`d.pass` is `strict` or `relaxed`. Relaxed means the strict gates (the reserve kept for the orchestrator itself, the per-lane gates) left nothing, so a held-back allowance is now used, up to a hard ceiling, for the same step. Nothing about the step (model, effort, read-only, same-family rule) changes. Mention it in one line.

## Never delegated

Requirements and scope, design and architecture, game-design judgement, the plan and its split into sub-tasks, whether a result is accepted, what becomes a new ticket. A worker may be asked for options or findings; the choice is the orchestrator's.

## Rules the script cannot enforce

1. **Brief like a ticket:** goal, files, constraints, acceptance check, "no commits, no pushing, do not weaken tests". Workers do not see this session; name the project rules the task needs.
2. **Never trust a worker's summary.** Rerun the acceptance check from the brief; read the diff yourself on a risk flag: public API change, test weakened or deleted, suppression added, files outside the brief.
3. **Failed fix:** resume the same worker thread with the findings. Two failed fixes: rethink the plan, then use role `hard`.
4. **Parallel writers need separate git worktrees.** Parallel sessions share each lane's allowance.
5. **Cheap read-only questions** (one grep, one short file) need no worker.
6. **The project's runbook wins** on how a worker runs there (sandbox limits, build flags, forbidden commands): read it before the first brief.
7. Tell the user in one line when a lane goes out, comes back, or the router uses the relaxed pass.

## Extending

Add a model, lane or role by editing `routing.json`: a lane names its family, billing kind (`subscription` or `credit`), a `reader` (how the script learns its state) and a `runner` (what `d.how` looks like). A new provider needs one reader function and one runner in `route.py`; chains name families, not lanes, so they need no change. Per-user settings (thresholds, caps, `claude_weekly_reset` such as `"Sun 17:00"`, `credit.reset_day`) go in `config.json` in the config directory (`~/.config/delegate`, or `%APPDATA%\delegate`), which also holds `state.json`, `ledger.jsonl` and the API key file. Tests: `python3 -m unittest discover -s tests` in the plugin root.
