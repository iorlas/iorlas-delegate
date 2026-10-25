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
https://github.com/iorlas/iorlas-task-loop/blob/main/docs/contracts/worker-run-v2.md (v2 since 0.29.0; v1 is gone, no shim)

- Handshake: `route.py contract --json` prints one line `{worker_run: 2, features, reasons{reason: next}, lanes{codex, claude, both}}`; a caller
  requires `worker_run == 2`. `lanes` is the static statement of what a worker sees and what cannot be blocked.
- Every answer that is not `done` or `agent` carries `reason` and `next`. `next` is the caller's whole policy: `retry` (after `retry_after`),
  `owner` (a person must act: log in, set up, verify a denial), `fail` (this attempt is over). `reason` is for wording and stats, never for branching.
  Reasons: lane_out, busy, duplicate_task, lock, vendor_prework, network, no_lane, logged_out -> retry; setup, secrets, bad_request, crash -> owner;
  caller, timeout, worker_failed, schema_invalid -> fail. A retry reason with no `retry_after` (no lane will come back by itself) answers `owner`.
  `busy` (all of a lane's slots taken, also for a `--resume` on that lane) is `stop` with `retry_after` = when a slot is expected; `lane_out` means the
  lane itself is out (limit refusal, or over its usage ceiling), with `retry_after` = the reset of the window it is over. A limit refusal mid-run (matched on Codex's
  error event or stderr, never the model's answer) marks the lane out until the time the message names ("try again at 5:50 AM"), else the
  window a fresh reading shows full, else the soonest window reset, else an hour. A Codex reading taken after the refusal that shows room
  (both windows under 95%) drops it. Since 0.35.4. Every answer given without launching a worker is a history.jsonl line too
  (`run_id` null, `reason`, `retry_after`, `resume`).
- `rung` (the chosen step's place in the role's chain, 0 = first choice), `fallback` (true when it is not the first choice) and
  `fallback_reason` (busy, lane_out, ceiling) and `first_lane` (the first choice's lane, set only on a fallback) on every answer that picked a lane; a resume repeats the original run's. Unattended, a first choice
  that is only busy (slots full, otherwise usable) answers `stop`/`busy`/retry with its retry_after instead of falling to the next rung;
  `--no-wait` falls through. Since 0.32.0.
- Roles own models: the caller picks the role, never the model. `--claude-model` (0.33.0) was removed in 0.34.0; the reviewer's Claude rungs run Sonnet since 0.34.0.
- `context` (per run, scripts only; interactive answers leave it out): `{instructions: [repo instruction files given], env_keys: [names passed]}`;
  the credit lane's key is passed but not listed.
- With `--schema`, delegate checks the answer (type, required, properties, items, enum), re-asks once in the same thread, and answers
  `failed`/`schema_invalid` when it still does not fit; tokens and denials of both turns add up.
- stdout is exactly one JSON object (`v` = 2, `status`, `reason`, `next`, `context`, `data`, `text`, `ref`, `lane`, `model`, `effort`, `family`, `flag`, `cost`, `tokens`, `secs`, `run_id`,
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
- Git is the coordinator's on every Claude step: the guard blocks, per piece of a command line, commit, push, checkout, switch, merge,
  rebase, reset, cherry-pick, revert, am, pull, and stash/branch/tag/worktree except their listing forms (stash list/show, branch -a/--contains/...,
  tag -l, worktree list). A blocked git write is policy and stays out of `denied`. (Before 0.31.4 Claude's own --disallowedTools patterns also
  refused `git stash list` and `git branch --contains`, and such a block counted as a denial.)
- Read-only Claude steps (reviewer, planner, light, not --host) get Bash limited by the guard (`route.py guard --read-only`) to reading commands:
  git diff/log/show/status/blame/grep/ls-files..., rg, grep, cat, head, tail, ls, find (no -exec/-delete), sed (no -i), wc, sort, diff, jq; no `>`, no
  `$(...)`. A blocked write is policy and stays out of `denied` (a secret path stays in). Codex read steps read freely inside their read-only sandbox.
- Secrets tripwire (delegate-internal): every worker prompt, and `brief_prefix` for interactive Agent-tool steps, starts with a rule never to touch credential files (~/.ssh, .env but not .env.example, ~/.aws, ~/.azure,
  ~/.gnupg, ~/.config/gh, ~/.docker/config.json, ~/.netrc, ~/.git-credentials, keychains). Claude steps get a PreToolUse hook (`route.py guard`, passed with `--settings`,
  which `--restricted` still loads) that blocks a tool call whose command or path matches; the block shows up in `denied`. Codex under exec has no hook: when a command
  that matches starts, delegate stops the worker and answers `failed` with the command in `denied`. Paths a command only excludes (`rg --glob '!**/.env*'`, `grep --exclude`, `find -not -path`, git `':!x'`) do not count.
  A regex, so an accident filter: a worker can spell a path around it.
- `denied: [{tool, input}]` (top level): commands the Claude auto-mode classifier refused (minus git writes the coordinator forbids anyway), or escalations Codex's reviewer
  refused (`command_execution` items with status `declined`; `input.command` is Codex's raw shell line). No new status: it rides on `done`/`failed`.
  Both are accident filters, not a boundary: a brief that explicitly asks for a command can get it approved.
- What a worker sees, both lanes: delegate's rules (secrets, git, escalation), the brief, and the repo's instruction files (in each folder from the
  repo root down to cwd, AGENTS.md, else CLAUDE.md; Claude gets them appended because --restricted would drop them). Codex: every skill it would
  list (its system skills, ~/.codex/skills, ~/.agents/skills, the repo's .agents/.codex skills) is turned off by path with `skills.config`; no user
  config, rules, MCP or hooks. Claude: no user or repo settings, hooks, plugins, skills or MCP. Codex sub-agents cannot be turned off (a brief that
  asks gets one; verified 2026-10-21); the prompt asks not to, and their tokens count as child threads.
- The Grok lane (GROK_SUBSCRIPTION, family `xai`, agent_runs kind `opencode`, macOS only, since 0.36.0) runs `opencode run --pure` with grok-4.7 on the
  SuperGrok login. OpenCode gets an empty config root (XDG_CONFIG_HOME under delegate's folder: the user's MCP servers, providers and permissions stay
  out), no project config, external or Claude Code skills. Its $SHELL is a script that runs every command under sandbox-exec: no network, no secret
  paths, no .env files (.env.example and friends stay readable), writes only in cwd and temp (none in cwd on read-only steps). OpenCode itself runs
  outside the sandbox, because it needs the network for the model and macOS cannot nest sandboxes; its own tools are limited by permissions (no web,
  no .env reads, nothing outside cwd, no sub-agents, no edits on read-only steps). setuid tools (ps) and localhost sockets fail inside the sandbox.
  No usage reading: only a refusal marks it out. `--schema` goes into the prompt and the usual re-ask checks it. Repo AGENTS.md is read by OpenCode;
  a repo with only CLAUDE.md gets it appended to the prompt.
  `transcript` is the session exported after the run (`opencode export`) to `opencode-sessions/<ses_id>.json` in delegate's folder; the agent_runs
  record carries it and no session or thread. History rows carry `transcript` for every lane since 0.36.2.
- Codex `tokens` include the automatic reviewer: it runs as child threads of the worker's thread, each in its own rollout file under `$CODEX_HOME/sessions/Y/M/D`
  (`session_meta.payload.parent_thread_id` = the worker's thread; the last `token_count` holds the total); `codex exec --json` never reports them. Since 0.26.0.
- A resumed Codex thread's `turn.completed` usage is the thread's running total; `exec resume` subtracts the total its rollout file held before
  the run, so each run counts only its own tokens (lifesim-fld.27.10: five resumes logged 43M for a 16M thread).
- `stop` (not `error`) answers a state-lock timeout, with `retry_after`; `error` means permanent. A Codex turn that fails (`turn.failed`/`error` event) before any
  work answers `stop` with `retry_after` and Codex's own message (network down, the Mac waking from sleep); after work it is `failed`; an HTTP 4xx other than 429 is `error`.
- Stopping: on SIGTERM, SIGHUP or Ctrl-C route.py kills the worker's process group before it exits. A SIGKILL on route.py cannot be caught, so
  the worker (its own process group, pgid = pid) keeps running: route.py prints `delegate: worker pid=N pgid=N run_id=R` on stderr right after
  the worker starts, and the lease shows it (`status --json` running[].worker_pid); a caller that SIGKILLs route.py kills that group too.
  Claude starts run_in_background shells in groups of their own: route.py's stop kills every descendant's group, a caller killing only
  the worker's group misses them (SIGTERM route.py instead). A Claude worker is stopped 5 s after its first `result` (stream-json): claude -p
  otherwise waits for its background tasks and takes another turn when they exit. Since 0.35.2.
  Each later `run` checks the leases: a worker whose route.py process is gone (pid plus start time; an expired lease of a living route.py,
  e.g. after the laptop slept, stays taken) and that is still the same process (pid plus start time) is stopped (SIGTERM, then SIGKILL) and logged in history.jsonl as `reaped`/`orphan_worker` before anything else starts; if it
  cannot be stopped its lease stays, so the same task answers duplicate_task and the lane counts it. POSIX only.
- Slots: at most `max_parallel` (setting, default 4; one when the lane's 5-hour use is at `strain`) runs in flight per lane. A full lane is skipped like a refused one; when nothing
  is left the answer is `stop` (or `lane_out`) with a `retry_after` 60-120 s ahead, jittered. `run` never waits for a slot. A run holds a lease in `state.json`
  (`leases`; 120 s, renewed every 30 s, so a killed `route.py` frees its slot by itself). With `--task ID`, a second `run` for the same task while the first is alive answers `stop`.
- SIGTERM, SIGHUP or Ctrl-C on `route.py run` stops its worker too (workers run in their own process group, so nothing else would). With `--unattended` a changed parent pid
  (the engine was SIGKILLed) does the same. Without this, an orphaned Codex keeps burning the plan.
- `route.py status --json` (v1, one line since 0.29.0, the dashboard's only read; `recent` rows carry `reason`): `{v, ts, lanes{name:{family,kind,out,out_until,reason,five,week,pace,burn,spend,cap,notes}}, running[{run_id,lane,role,task,started,age_s}],
  recent[{run_id,ts,role,task,lane,model,status,host,secs,cost,tokens,denials}] (last `--recent N`, default 20), needs_input[]}`. `five`/`week` are null when unknown. `doctor --json` carries `v: 1`. Dashboards read these, never
  `state.json` or `history.jsonl`.
  A lane's `reason` (when `out`): limit, logged_out, busy or error. Since 0.35.1.
- `history.jsonl` (config dir, private; rotated: lines older than 14 days are dropped once it passes 1 MB): one line per finished run: role, task, lane, model, effort, status, secs, cost, tokens, 5-hour and weekly use before and after, ids of runs
  that overlapped it, slots. For calibration; never the brief. Not part of the contract.
- `route.py status --claude-usage '<get_usage plan.windows JSON>'` is part of the contract: it stores the reading for later `run`s. Keep that command and input shape, or bump `v`.
- `reviewer`: `--authored-by openai|claude|xai` is optional (the family that wrote the diff; `xai` since 0.36.0). Another family reviews first (Codex wherever it is another family and for unknown authors, Grok for Codex's work, since 0.37.0); the same family is the last resort
  and comes back with `flag: "same_family"` and a one-line `warning` (stderr, and in the `run` result; since 0.38.0), never needing consent; with no reviewer at all the answer is `stop`, never an exception.
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


### Dashboard routing snapshot

`route.py status --json` (v1) optionally includes `routing` (v1): `at`, `caller: unattended`,
`lanes` (id, label, family, billing, enabled, available, reason, unattended, until, five/week used percentages, model names),
and `roles` (id, about, advance, selected, chain). `selected` is the current fresh-call decision under the same
unattended rules, busy-slot limits and usage gates as `run`; it never starts a worker. Credit lanes are inventory
only for unattended callers. `chain` contains ordered model/effort/family/mode/flag/rung/lanes steps; reviews
also include `by_author` scenarios. Planning degradation still requires consent; premium models are not part of
normal chains. This is a preview: limits can change before dispatch and resumed threads stay on their own lane.
Consumers must tolerate a missing snapshot from older delegate installations. No credentials, private configuration
or account subscription tier are exported; a configured login is not proof of a particular paid plan.
