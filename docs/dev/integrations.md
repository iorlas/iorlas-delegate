# What other tools rely on (read before changing delegate)

Read this before changing how delegate launches workers, computes limits, or stores state. A plugin install ships
only `skills/` and `commands/`, so nothing here is loaded when someone just uses delegate.

Delegate has users besides the orchestrating session: today the iorlas-task-loop plugin's dashboard. They depend on
**two things only**. Everything else (route.py's internals, `routing.json`, `state.json`, the ledger, mode files,
command names and flags) is private and may change in any release.

## 1. The agent-runs v1 records (a public format)

Spec, owned by the consumer side: https://github.com/iorlas/iorlas-task-loop/blob/main/docs/contracts/agent-runs-v1.md

Delegate writes them from one file, `skills/delegate/agent_runs.py`, the only code that knows the format:

| call site | what it publishes | why the dashboard needs it |
|---|---|---|
| `launch_codex` / `launch_claude` (route.py), before the worker starts: `agent_runs.run(id, kind="codex"\|"claude-p", lane, model, cwd, parent_session, title[, task], started, status="running")`; codex adds `thread` as soon as `thread.started` arrives, claude passes `session=ref` | a run record | ties the headless worker to the session (and so the task) that launched it; `session`/`thread` find its live log |
| `cmd_run`, after it: `agent_runs.run(id, ended, status, transcript)` | the same record, finished | shows done or failed and how long it ran |
| `publish_lanes(lanes, st, t)` after `gather()` in `cmd_status` and `cmd_next` | `lanes/delegate.json` | the limits chips (5h and weekly use, a lane that is out and why); a reader ignores it once it is 6 hours old |

`claude -p` runs pass `--session-id <id>` (not `--no-session-persistence`) so that the worker's transcript exists at
`~/.claude/projects/*/<id>.jsonl`. Without it the run record still shows, with no log behind it.

Codex runs and Agent-tool subagents need nothing from delegate: the dashboard finds them in the codex plugin's job
state and in Claude Code's own transcripts.

## 2. `route.py run` is a contract; everything else is not

task-loop's engine calls `route.py run ROLE --brief FILE --cwd DIR [--schema F] [--resume REF] [--cap USD] [--timeout S] [--parent SESSION] [--task ID] [--title T]`
as a script, with no Claude session behind it. The spec belongs to the consumer:
https://github.com/iorlas/iorlas-task-loop/blob/main/docs/contracts/worker-run-v1.md

- stdout is exactly one JSON object (`v`, `status`, `data`, `text`, `ref`, `lane`, `model`, `effort`, `family`, `flag`, `cost`, `secs`, `run_id`,
  `transcript`, `retry_after`, `say`, `git_violation`); exit 0 whenever it was printed. Statuses: `done`, `failed`, `timeout`, `lane_out`, `stop`, `error`
  (plus `agent` for interactive callers only).
- Headless = no `CLAUDE_CODE_ENTRYPOINT` (the engine strips `CLAUDE_*`). Headless never gets `agent`, `ask_user` or `needs_input`; CLAUDE_CREDIT is never
  picked; CLAUDE_SUBSCRIPTION runs `claude -p` on the logged-in account, and only while a fresh reading of its use exists (`~/.claude/state/max-limits.json`
  from the status line, or `state.json`, newer than `usage_stale_minutes`); otherwise `stop` with `retry_after`. `orchestrator_reserve` applies.
- `route.py status --claude-usage '<get_usage plan.windows JSON>'` is part of the contract: it stores the reading, and a headless `run` uses it for
  `usage_stale_minutes` (30). Keep that command and input shape, or bump `v`.
- `reviewer`: `--writer openai|claude` is optional (the family that wrote the diff). The other family reviews first; the same family is the last resort
  and comes back with `flag: "same_family"`; with no reviewer at all the answer is `stop`, never an exception.
- Everything else (`next`, `doctor`, flags not listed above, `state.json`, `routing.json`, mode files) is private and may change in any release. Changing
  the contract above means changing the spec in iorlas-task-loop first.

## If you rewrite delegate

- Launching workers somewhere new (a new lane or runner): call `agent_runs.run(...)` there
  with the required fields, once at start and once at the end. Codex launched outside the codex plugin: set `kind`
  to `codex` and `thread` to its thread id.
- Limits computed somewhere new: call `agent_runs.lanes(...)` with the new numbers, at least whenever `next` runs.
- Deleting `agent_runs.py` is allowed, and nothing breaks: the dashboard then shows no delegate runs or limits.
  Say so in the release notes.
- A field's meaning changes, or a required field goes away: that is agent-runs v2. Change the spec in
  iorlas-task-loop first, make its reader accept both versions, then switch delegate.
- Never make delegate read anything of task-loop's, and never write outside `agent-runs/` for a dashboard's sake.

Tests: `tests/test_route.py`, class `AgentRuns` (the records' shape). Keep it green or replace it with an equivalent.
