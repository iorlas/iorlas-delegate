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
| `launch_codex` / `launch_claude` (route.py), before the worker starts: `agent_runs.run(id, kind="codex"\|"claude-p", lane, model, cwd, parent_session, title[, task], started, status="running")`; codex adds `thread` as soon as `thread.started` arrives, claude passes `session=ref` | a run record | ties the unattended worker to the session (and so the task) that launched it; `session`/`thread` find its live log |
| `cmd_run`, after it: `agent_runs.run(id, ended, status, transcript)` | the same record, finished | shows done or failed and how long it ran |
| `publish_lanes(lanes, st, t)` after `gather()` in `cmd_status` and `cmd_next` | `lanes/delegate.json` | the limits chips (5h and weekly use, a lane that is out and why); a reader ignores it once it is 6 hours old |

`claude -p` runs pass `--session-id <id>` (not `--no-session-persistence`) so that the worker's transcript exists at
`~/.claude/projects/*/<id>.jsonl`. Without it the run record still shows, with no log behind it.

Codex runs and Agent-tool subagents need nothing from delegate: the dashboard finds them in the codex plugin's job
state and in Claude Code's own transcripts.

## 2. `route.py run` is a contract; everything else is not

task-loop's engine calls `route.py run ROLE --unattended --brief FILE --cwd DIR [--schema F] [--resume REF] [--cap USD] [--timeout S] [--parent SESSION] [--task ID] [--title T]`
as a script, with no Claude session behind it. The spec belongs to the consumer:
https://github.com/iorlas/iorlas-task-loop/blob/main/docs/contracts/worker-run-v1.md

- stdout is exactly one JSON object (`v`, `status`, `data`, `text`, `ref`, `lane`, `model`, `effort`, `family`, `flag`, `cost`, `tokens`, `secs`, `run_id`,
  `transcript`, `retry_after`, `say`, `git_violation`); `tokens` is `{in, cached, out}` or null, and `in` counts all input including cache reads
  (and Claude cache writes; before 0.22.0 history recorded Claude's uncached input only); exit 0 whenever it was printed. Statuses: `done`, `failed`, `timeout`, `lane_out`, `stop`, `error`
  (plus `agent` for interactive callers only).
- `--unattended` is how an engine says no Claude session is behind it. Without it the caller is a Claude session and may get `agent`. With it: never `agent`, `ask_user`
  or `needs_input`; CLAUDE_CREDIT is never picked; CLAUDE_SUBSCRIPTION runs `claude -p` on the logged-in account. No time or token limit by default: `--timeout` is
  opt-in and an unknown plan reading does not stop a run. `orchestrator_reserve` still applies when a reading exists.
- `--host` (one boolean, the caller's explicit consent): "this work cannot run in a sandbox" (Unity). Any lane takes it. Claude steps get Bash (a read-only step too) and an
  interactive caller runs `claude -p` instead of the Agent tool, so the classifier applies. Codex runs the same as without it (below). `--resume` must repeat the original run's
  `--host`, else `error`. 0.23 sent host work to `settings.host_lanes` (default Claude only); 0.24 drops that setting.
- Everything else about access is delegate-internal and fixed (never set by the caller or a repo file). Codex, every run and resume: `-s workspace-write` (write steps) or
  `-s read-only`, no network, no extra writable folders, `--ignore-user-config --ignore-rules`, and `approval_policy=on-request` + `approvals_reviewer=auto_review`: a command
  the sandbox blocks may ask to run outside it, and Codex's automatic reviewer (a model, no human) approves or refuses that one command. Verified 2026-10-20 (codex-cli 0.162.1):
  `dotnet test` with a new NuGet package and a Unity 6000.7 batchmode compile were approved and ran; posting a `.env` to an outside host was refused. Codex permission profiles
  (`default_permissions`) can block reads such as `~/.ssh`, but an approved escalation under a profile stays sandboxed (Unity fails), so delegate does not use them; network off
  is what keeps a read secret from leaving without review. Claude always gets `--restricted --tools <list> --strict-mcp-config --permission-mode auto --permission-prompts none` (the repo's and the
  user's hooks, plugins and settings do not load; subscription login still works; never `--bare`, it disables login). Worker env drops credential-looking variables.
- Secrets tripwire (delegate-internal): every worker prompt starts with a rule never to touch credential files (~/.ssh, .env but not .env.example, ~/.aws, ~/.azure,
  ~/.gnupg, ~/.config/gh, ~/.docker/config.json, ~/.netrc, ~/.git-credentials, keychains). Claude steps get a PreToolUse hook (`route.py guard`, passed with `--settings`,
  which `--restricted` still loads) that blocks a tool call whose command or path matches; the block shows up in `denied`. Codex under exec has no hook: when a command
  that matches starts, delegate stops the worker and answers `failed` with the command in `denied`. A regex, so an accident filter: a worker can spell a path around it.
- `denied: [{tool, input}]` (top level): commands the Claude auto-mode classifier refused (minus git writes the coordinator forbids anyway), or escalations Codex's reviewer
  refused (`command_execution` items with status `declined`; `input.command` is Codex's raw shell line). No new status: it rides on `done`/`failed`.
  Both are accident filters, not a boundary: a brief that explicitly asks for a command can get it approved. Reviewer turns run as separate Codex threads, so `tokens` does not count them.
- `stop` (not `error`) answers a state-lock timeout, with `retry_after`; `error` means permanent.
- Slots: at most `max_parallel` (setting, default 4; one when the lane's 5-hour use is at `strain`) runs in flight per lane. A full lane is skipped like a refused one; when nothing
  is left the answer is `stop` (or `lane_out`) with a `retry_after` 60-120 s ahead, jittered. `run` never waits for a slot. A run holds a lease in `state.json`
  (`leases`; 120 s, renewed every 30 s, so a killed `route.py` frees its slot by itself). With `--task ID`, a second `run` for the same task while the first is alive answers `stop`.
- SIGTERM, SIGHUP or Ctrl-C on `route.py run` stops its worker too (workers run in their own process group, so nothing else would). With `--unattended` a changed parent pid
  (the engine was SIGKILLed) does the same. Without this, an orphaned Codex keeps burning the plan.
- `route.py status --json` (v1, the dashboard's only read): `{v, ts, lanes{name:{family,kind,out,out_until,five,week,pace,burn,spend,cap,notes}}, running[{run_id,lane,role,task,started,age_s}],
  recent[{run_id,ts,role,task,lane,model,status,host,secs,cost,tokens,denials}] (last `--recent N`, default 20), needs_input[]}`. `five`/`week` are null when unknown. `doctor --json` carries `v: 1`. Dashboards read these, never
  `state.json` or `history.jsonl`.
- `history.jsonl` (config dir, private; rotated: lines older than 14 days are dropped once it passes 1 MB): one line per finished run: role, task, lane, model, effort, status, secs, cost, tokens, 5-hour and weekly use before and after, ids of runs
  that overlapped it, slots. For calibration; never the brief. Not part of the contract.
- `route.py status --claude-usage '<get_usage plan.windows JSON>'` is part of the contract: it stores the reading for later `run`s. Keep that command and input shape, or bump `v`.
- `reviewer`: `--authored-by openai|claude` is optional (the family that wrote the diff). The other family reviews first; the same family is the last resort
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
