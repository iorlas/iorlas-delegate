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
as a script, with no Claude session behind it. The contract is `skills/delegate/contract.json` (reasons, facts, flags, answer schema); the consumer is iorlas-task-loop.

- Handshake: `route.py contract --json` prints one line `{contract: 3, reasons{reason: {scope, permanence, retry, health, wording}}, run_args[]}`; a caller
  requires `contract == 3`. What a worker sees and what cannot be blocked is stated once, in "What a worker sees" below.
- Every answer carries `stop`: `null` when it is done (or the interactive handoff to the Agent tool, which sets `agent_model`), else
  `{reason, scope, permanence, retry{kind, at}, ref, fix?, resumable?, partial?}`. The caller's whole policy is `(scope, permanence, retry.kind)`; `reason` is for wording and stats.
  scope: `bead`, `caller`, `machine`, `lane`, `model`. permanence: `transient`, `needs_owner` (a person must act: log in, set up, verify a denial). retry.kind: `none`, `predicted`
  (`at` = when the lane or hold ends), `poll` (`at` = look again about then, 60-120 s). A predicted row with no time answers `needs_owner`/`none`. `ref` is the vendor session when
  one existed; `resumable` and `partial` ride on `network` and `killed`; `fix` is set where delegate knows the cure. Scope `lane`/`model` is stamped by delegate alone (classifier or
  breaker); `bead`, `caller` and `machine` stops (network, killed, bad_request, secrets, timeout) never mark a lane. `busy` (all of a lane's slots taken, also for a `--resume` on that
  lane) polls with `at` = when a slot is expected; `rate_limited` means the lane itself is out (limit refusal, or over its usage ceiling), with `at` = the reset of the window it is over.
  A limit refusal mid-run (matched on Codex's error event or stderr, never the model's answer) marks the lane out until the time the message names ("try again at 5:50 AM"), else the
  window a fresh reading shows full, else the soonest window reset, else an hour. A Codex reading taken after the refusal that shows room
  (both windows under 95%) drops it. Every answer given without launching a worker is a history.jsonl line too
  (`run_id` null, `outcome`, `until`, `resume`).
- `rung` (the chosen step's place in the role's chain, 0 = first choice), `fallback` (true when it is not the first choice) and
  `fallback_reason` (busy, out, ceiling) and `first_lane` (the first choice's lane, set only on a fallback) on every answer that picked a lane; a resume repeats the original run's. Unattended, a first choice
  that is only busy (slots full, otherwise usable) answers `busy` with its `at` instead of falling to the next rung.
- Roles own models: the caller picks the role, never the model. The reviewer's Claude rungs run Sonnet.
- `context` (per run, scripts only; interactive answers leave it out): `{instructions: [repo instruction files given], env_keys: [names passed]}`;
  the credit lane's key is passed but not listed.
- With `--schema`, delegate checks the answer (type, required, properties, items, enum), re-asks once in the same thread, and answers
  `schema_invalid` when it still does not fit; tokens and denials of both turns add up.
- stdout is exactly one JSON object (`contract` = 3, `stop`, `context`, `data`, `text`, `ref`, `lane`, `model`, `effort`, `family`, `flag`, `cost`, `tokens`, `secs`, `run_id`,
  `transcript`, `say`, `git_violation`, plus `usage`, `call_id`, `transcript_info`, `transcript_status`; `agent_model`, `brief_prefix`, `cwd` on the interactive handoff); `tokens` is `{in, cached, out}` or null, and `in` counts all input including cache reads
  (and Claude cache writes); exit 0 whenever it was printed, a bad flag or argument of `run` included (`bad_request`, `fix` names the argparse message, no worker started).
- `--unattended` is how an engine says no Claude session is behind it. Without it the caller is a Claude session and may get `agent`. With it: never `agent`, `ask_user`
  or `needs_input`; CLAUDE_CREDIT is never picked; CLAUDE_SUBSCRIPTION runs `claude -p` on the logged-in account. No time or token limit by default: `--timeout` is
  opt-in and an unknown plan reading does not stop a run. `orchestrator_reserve` still applies when a reading exists.
- Everything else about access is delegate-internal and fixed (never set by the caller or a repo file). Codex, every run and resume: `-s workspace-write` (write steps) or
  `-s read-only`, no network, no extra writable folders, `--ignore-user-config --ignore-rules`, and `approval_policy=on-request` + `approvals_reviewer=auto_review`: a command
  the sandbox blocks may ask to run outside it, and Codex's automatic reviewer (a model, no human) approves or refuses that one command. Verified 2026-10-20 (codex-cli 0.162.1):
  `dotnet test` with a new NuGet package and a Unity 6000.7 batchmode compile were approved and ran; posting a `.env` to an outside host was refused. Codex permission profiles
  (`default_permissions`) can block reads such as `~/.ssh`, but an approved escalation under a profile stays sandboxed (Unity fails), so delegate does not use them; network off
  is what keeps a read secret from leaving without review. Claude always gets `--restricted --tools <list> --strict-mcp-config --permission-mode auto --permission-prompts none` (the repo's and the
  user's hooks, plugins and settings do not load; subscription login still works; never `--bare`, it disables login). Worker env drops credential-looking variables.
- Git is the coordinator's on every Claude step: the guard blocks, per piece of a command line, commit, push, checkout, switch, merge,
  rebase, reset, cherry-pick, revert, am, pull, and stash/branch/tag/worktree except their listing forms (stash list/show, branch -a/--contains/...,
  tag -l, worktree list). A blocked git write is policy and stays out of `denied`.
- Read-only Claude steps (reviewer, planner, light) get Bash limited by the guard (`route.py guard --read-only`) to reading commands:
  git diff/log/show/status/blame/grep/ls-files..., rg, grep, cat, head, tail, ls, find (no -exec/-delete), sed (no -i), wc, sort, diff, jq; no `>`, no
  `$(...)`. A blocked write is policy and stays out of `denied` (a secret path stays in). Codex read steps read freely inside their read-only sandbox.
- Secrets tripwire (delegate-internal): every worker prompt, and `brief_prefix` for interactive Agent-tool steps, starts with a rule never to touch credential files (~/.ssh, .env but not .env.example, ~/.aws, ~/.azure,
  ~/.gnupg, ~/.config/gh, ~/.docker/config.json, ~/.netrc, ~/.git-credentials, keychains). Claude steps get a PreToolUse hook (`route.py guard`, passed with `--settings`,
  which `--restricted` still loads) that blocks a tool call whose command or path matches; the block shows up in `denied`. Codex under exec has no hook: when a command
  that matches starts, delegate stops the worker and answers `failed` with the command in `denied`. Paths a command only excludes (`rg --glob '!**/.env*'`, `grep --exclude`, `find -not -path`, git `':!x'`) do not count.
  A regex, so an accident filter: a worker can spell a path around it.
- `denied: [{kind, tool, path, input}]` (top level): `kind` is `policy` (the Claude auto-mode classifier refused a call, minus git writes the coordinator forbids anyway; Codex's reviewer
  refused an escalation: `command_execution` items with status `declined`, `input.command` is Codex's raw shell line; OpenCode's permission rules refused a tool), `secret` (the call named a
  secret path; `path` is the match) or `sandbox_limit` (the sandbox blocked a call that needs a system resource: ps, setuid tools, localhost sockets). `sandbox_limit` is listed so a reader sees it
  and is never a failure: it is not counted in history `denials`. Denials ride on any answer. Both filters are accident filters, not a boundary: a brief that explicitly asks for a command can get it approved.
- What a worker sees, both lanes: delegate's rules (secrets, git, escalation), the brief, and the repo's instruction files (in each folder from the
  repo root down to cwd, AGENTS.md, else CLAUDE.md; Claude gets them appended because --restricted would drop them). Codex: every skill it would
  list (its system skills, ~/.codex/skills, ~/.agents/skills, the repo's .agents/.codex skills) is turned off by path with `skills.config`; no user
  config, rules, MCP or hooks. Claude: no user or repo settings, hooks, plugins, skills or MCP. Codex sub-agents cannot be turned off (a brief that
  asks gets one; verified 2026-10-21); the prompt asks not to, and their tokens count as child threads.
- The Grok lane (GROK_SUBSCRIPTION, family `xai`, agent_runs kind `opencode`, macOS only) runs `opencode run --pure` with grok-4.7 on the
  SuperGrok login. OpenCode gets an empty config root (XDG_CONFIG_HOME under delegate's folder: the user's MCP servers, providers and permissions stay
  out), no project config, external or Claude Code skills. Its $SHELL is a script that runs every command under sandbox-exec: no network, no secret
  paths, no .env files (.env.example and friends stay readable), writes only in cwd and temp (none in cwd on read-only steps). OpenCode itself runs
  outside the sandbox, because it needs the network for the model and macOS cannot nest sandboxes; its own tools are limited by permissions (no web,
  no .env reads, nothing outside cwd, no sub-agents, no edits on read-only steps). setuid tools (ps) and localhost sockets fail inside the sandbox.
  No usage reading: only a refusal marks it out. `--schema` goes into the prompt and the usual re-ask checks it. Repo AGENTS.md is read by OpenCode;
  a repo with only CLAUDE.md gets it appended to the prompt.
  `transcript` is the session exported after the run (`opencode export`) to `opencode-sessions/<ses_id>.json` in delegate's folder; the agent_runs
  record carries it and no session or thread. History rows carry `transcript` for every lane.
- Codex `tokens` include the automatic reviewer: it runs as child threads of the worker's thread, each in its own rollout file under `$CODEX_HOME/sessions/Y/M/D`
  (`session_meta.payload.parent_thread_id` = the worker's thread; the last `token_count` holds the total); `codex exec --json` never reports them.
- A resumed Codex thread's `turn.completed` usage is the thread's running total; `exec resume` subtracts the total its rollout file held before
  the run, so each run counts only its own tokens (lifesim-fld.27.10: five resumes logged 43M for a 16M thread).
- `usage` is per call, never cumulative (delegate-uk0, study 85): `usage.tokens` keeps `in/cached/out` and adds `in_fresh`, `cache_read`, `cache_write` (null where the
  vendor does not report it: Codex), `out`, and on Claude `cache_write_by_ttl {5m,1h}`. A resumed Codex thread subtracts the earlier cumulative total (its rollout file, else
  the `cumulative` field of the newest history row for that ref). Cost is what the harness returned, with no price table: Claude `total_cost_usd` on every lane
  (subscription included; top-level `cost` is no longer zeroed there), Codex `cost_usd: null`/`cost_source: "unknown"`, Grok the summed opencode step `cost`, zai `unknown`,
  free DeepSeek `0`/`free`. Also `ref`, `last_activity_at`, `warm_until`, `ttl_s`, `ttl_source`, `cost_usd`, `cost_source`, `accounting`. `warm_until` is telemetry
  (Claude subscription 3600 s, Claude credit 300 s, Codex 3600 s observed, opencode unknown = null): no routing reads it.
- A state-lock timeout answers `lock` (transient, poll). A Codex turn that fails (`turn.failed`/`error` event) before any
  work answers `vendor_blip` (transient, poll) with Codex's own message, or `network` when the message words a dropped connection or the probe finds the net down; after work it is `worker_failed`;
  an HTTP 4xx other than 401/403/429 is `bad_request`, 401/403 is `logged_out`, 429 is `rate_limited`.
- Stopping: on SIGTERM, SIGHUP or Ctrl-C route.py kills the worker's process group before it exits. A SIGKILL on route.py cannot be caught, so
  the worker (its own process group, pgid = pid) keeps running: route.py prints `delegate: worker pid=N pgid=N run_id=R` on stderr right after
  the worker starts, and the lease shows it (`status --json` running[].worker_pid); a caller that SIGKILLs route.py kills that group too.
  Claude starts run_in_background shells in groups of their own: route.py's stop kills every descendant's group, a caller killing only
  the worker's group misses them (SIGTERM route.py instead). A Claude worker is stopped 5 s after its first `result` (stream-json): claude -p
  otherwise waits for its background tasks and takes another turn when they exit.
  Each later `run` checks the leases: a worker whose route.py process is gone (pid plus start time; an expired lease of a living route.py,
  e.g. after the laptop slept, stays taken) and that is still the same process (pid plus start time) is stopped (SIGTERM, then SIGKILL) and logged in history.jsonl (`reaped: true`, or `false` when it could not be stopped) before anything else starts; if it
  cannot be stopped its lease stays, so the same task answers duplicate_task and the lane counts it. POSIX only.
- Slots: at most `max_parallel` (setting, default 4; one when the lane's 5-hour use is at `strain`) runs in flight per lane. A full lane is skipped like a refused one; when nothing
  is left the answer is `busy` (or `no_lane`) with `at` 60-120 s ahead, jittered. `run` never waits for a slot. A run holds a lease in `state.json`
  (`leases`; 120 s, renewed every 30 s, so a killed `route.py` frees its slot by itself). With `--task ID`, a second `run` for the same task while the first is alive answers `duplicate_task`.
- SIGTERM, SIGHUP or Ctrl-C on `route.py run` stops its worker too (workers run in their own process group, so nothing else would). With `--unattended` a changed parent pid
  (the engine was SIGKILLed) does the same. Without this, an orphaned Codex keeps burning the plan.
- `route.py status --json` (v1, one line, the dashboard's only read): `{v, ts, lanes{name:{family,kind,out,out_until,reason,five,week,pace,burn,spend,cap,notes}}, running[{run_id,lane,role,task,started,age_s}],
  needs_input[], cockpit}`. `cockpit` (`route.py status` prints it as text): `{v, at, legend, next_worker{action,lane,model,effort,pass,fallback,why}, lanes[{id,label,family,kind,model,state,emoji,reason,detail,unlock{locked,known,at,in_s,text,why},windows[{name,used_pct,resets_at,resets_in_s,source,age_s}],warnings[],low_tokens[],meter,login{ok,why,checked_at},running}]}`, one row for every lane in routing.json, configured or not; `state` is ready, resting, logged_out, spend, slow, off or unknown; login checks (`codex login status`, `claude auth status`, opencode's auth file) are cached 5 minutes in state.json `auth` and never call a model. Every login answer (a non-cheap `status`, `doctor`, a successful `run`) is also written to `auth-cache.json` (config dir, TTL 10 min); `next` reads it instead of spawning `claude auth status`. `five`/`week` are null when unknown. `doctor --json` carries `v: 1`. `route.py status --json --cheap`: zero process spawns, no network, no keychain: it prints the last full `status` (kept in `status-snapshot.json`) with `cheap: true`, `ts` (now), `age_s` (seconds since that snapshot; `null` when no full status has run), `running` re-read from the leases, and `auth{lane:{ok,why,age_s}}` from `auth-cache.json`. Read `age_s` to decide whether to pay for a full `status`. A non-cheap `status` writes the snapshot. Dashboards read these, never
  `state.json` or `history.jsonl`.
  A lane's `reason` (when `out`): limit, logged_out, busy or error.
- `history.jsonl` (config dir, private; rotated: lines older than 14 days are dropped once it passes 1 MB): one line per finished run: role, task, lane, model, effort, done or outcome, secs, cost, tokens, 5-hour and weekly use before and after, ids of runs
  that overlapped it, slots. For calibration; never the brief. Not part of the contract.
- `route.py status --claude-usage '<get_usage plan.windows JSON>'` is part of the contract: it stores the reading for later `run`s. Keep that command and input shape, or bump `v`.
- `reviewer`: `--authored-by openai|claude|xai|deepseek|zai` is optional (the family that wrote the diff). Order (delegate-qjw, delegate-pnz): Codex first wherever it is another family; then the other non-Claude
  families, cheapest first by the `cost` configured per model in `routing.json` (roles with `"order": "cost"`; never by name); then the author's own non-Claude family; then Claude Sonnet last. The same-family review
  comes back with `flag: "same_family"` and a one-line `warning` (stderr, and in the `run` result), never needing consent; with no reviewer at all the answer is `no_lane`, never an exception.
  Grok/opencode (family `xai`) never reviews, even when it is the only lane up (the role's `exclude_families`; study 81: 74% of review tokens, same send-back rate as Sonnet, ~10x wall time); it still routes for worker.
- Claude Opus never runs in the loop, for any role: models with `"loop": false` in `routing.json` (opus-5.5) are dropped from every chain and every consent list. A role left with no rung answers
  `no_lane` with a `say` naming the excluded model. Opus stays a model an explicit user call outside the loop may pick (the router never chooses it).
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
No credentials, private configuration
or account subscription tier are exported; a configured login is not proof of a particular paid plan.

### Live event file

With `DELEGATE_LIVE_FILE=<path>` set for a `route.py run`, every worker launch appends one JSON object per line to that path (parents created, flushed per
line); unset, nothing is written. `{"t": epoch, "k": kind, ...}`. Kinds: `start` (harness, lane, model, effort, role, pid), `cmd` (id, text <= 2000, cwd if known) /
`cmd_end` (id, exit, out = last 1500 chars, secs), `tool` / `tool_end` (same, non-shell tools; text = name + main arg), `msg` (<= 2000), `think` (<= 500),
`tokens` (in, cached, out), `stall` (secs; at 60 s of silence, then every 60 s), `end` (status done|timeout|killed|error, secs, cost_usd), `raw` (type name
of an unmapped event, never its payload). A write error warns once on stderr and stops the feed; the call is unaffected. Code: `skills/delegate/lanes/live.py`.

## 6. The worker's life belongs to delegate (OWN)

- A run answer carries `worker` = `{end: ok|error|timeout|worker_survived, stopped, term_sent, kill_sent, waited_s, survivors[{pid,start}]}` (typed in `skills/delegate/contracts.py`, which task-loop vendors; `RunRequest.argv()` is the flag list, `RunResult.parse()` the reader).
- Before delegate exits it stops the worker's whole tree (`lanes/reaper.py`): SIGTERM, `DELEGATE_STOP_GRACE` s (5), SIGKILL, re-look. A pid still alive after that is a survivor; a done answer with a survivor becomes stop reason `worker_survived`.
- SIGTERM/SIGHUP to `route.py run` stops the tree first, then answers `killed` with exit 0. `run --idle N` sets the silence limit (default per CLI, `lanes/supervisor.py`); the live JSONL (`DELEGATE_LIVE_FILE`) is unchanged.
- `osproc.py` is the only module that lists processes (/proc on Linux, `ps` on macOS; Windows answers "nothing found"). Tests: `python dev/suite.py` (sharded units, per-test home via `tests/hermetic.py`), `python dev/suite.py --contract` (real processes, fake agent `tests/fakes/fake_agent.py`).
