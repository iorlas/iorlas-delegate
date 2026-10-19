# Constitution

Read this before changing anything in this repo. Rules are Denis's; the lessons are what broke or surprised us. Add to it when something new is learned. One fact lives in one place: if a rule moves, delete it from where it was.

## Rules

1. **No duplication, no redundancy.** One way to do each thing; one place for each fact and each copy of data (never two copies of a key or a setting).
2. **No backward compatibility** (no shims, aliases, deprecated flags, migrations, old-format readers) **until Denis says the plugin has been given to someone else.** A public repo does not change this. When a thing changes: change its callers, delete the old form, fix his own machine by hand.
3. **Small and simple.** Less code, fewer concepts, fewer commands. Prefer deleting to adding.
4. **Instructions are strict, terse, constrained.** Everything the orchestrator reads (SKILL.md, modes, script output) costs tokens in every session, and delegate competes with the built-in sub-agent, whose tool definition is paid anyway. Judge delegate by its marginal tokens. Budgets are enforced by `TokenBudget` in `tests/test_route.py`; ratchet them down, never up. Worker tokens are out of scope.
5. **The script decides.** The model supplies only what the script cannot read (today: Desktop usage from `get_usage`). Portability lives in the script, never in per-OS branches in markdown.
6. **Workers never touch git.** The coordinator owns branches, worktrees, commits, merges.
7. **Nothing blocks delegation.** Setup is optional; sub-agent-only works with nothing installed; a lane nobody set up is offered once, never nagged.
8. **The API key never enters chat.** Env var, `set-key` in the user's terminal, or `set-key --web`.
9. **Claim only what was checked.** Say "built" vs "bead"; report test and CI status of the exact commit.
10. **Read `docs/dev/integrations.md`** before changing how workers launch or how limits are computed (agent-runs v1 is a public format).

## Lessons

- `codex exec` waits forever on an open stdin pipe; give it the brief on stdin with `-` and close it. `-o FILE` returns only the final message. A non-git directory needs `--skip-git-repo-check`. Killing only the `codex` wrapper leaves the vendor binary running: kill the process group.
- `codex exec --json` prints JSONL; `thread.started` carries `thread_id` and arrives first. `codex exec resume` takes `-m`, `-c`, `-o`, `--json`, `--output-schema`, `--skip-git-repo-check` but not `-s`/`-C`: set the sandbox with `-c sandbox_mode="..."` and the folder with the process cwd. `claude -p --output-format json` returns `result`, `session_id`, `total_cost_usd`, `structured_output` (with `--json-schema`); `--session-id` fixes the id up front, `--resume ID` continues it. Both verified live 2026-10-18.
- Unattended is one explicit flag, `run --unattended`, never derived from the environment (a contract that depends on the caller stripping `CLAUDE_*` breaks silently). Unattended callers have no Agent tool: the subscription lane runs `claude -p` on the login, with no time or token limit of its own (the owner's decision 2026-10-19; limits come later from real stats); credit lanes are never picked.
- `locked()` must raise on timeout, never run the update unlocked. Slots are leases that expire (pid-liveness checks are unsafe on Windows: `os.kill(pid, 0)` can terminate). Workers run in their own process group, so `route.py` must stop them itself on SIGTERM/SIGHUP/Ctrl-C and, unattended, when the parent dies.
- Parallel slots of one repo change refs and worktrees while a worker runs; compare only the cwd's HEAD and branch.
- macOS has no `timeout`; use `subprocess` timeouts.
- `HTTPServer.server_bind` does a reverse-DNS lookup that stalls for seconds on macOS runners; skip it.
- `ipaddress`: `0.0.0.0` is `is_private`; check `is_unspecified`.
- `CLAUDE_PLUGIN_DATA` names whichever plugin launched the process (here: Codex's); never read it. Claude Code does not export plugin variables to the model's Bash calls; derive the plugin data dir from the install path (`plugins/cache/<marketplace>/<plugin>/<version>` -> `plugins/data/<plugin>-<marketplace>`).
- `get_usage` returns `plan.windows[{label, percentUsed, resetsAt}]`. Labels: "5-hour limit", "Weekly · all models", "Weekly · Fable".
- Codex's marketplace reader ignores `"source": "github"` entries; use the `url` form.
- Plugin skills cannot be hidden from the skill listing; `modes/*.md` inside the skill folder is the way to keep instructions out of context until needed.
- Tests must not depend on the machine: isolate `HOME`, `XDG_CONFIG_HOME`, `DELEGATE_CONFIG_DIR`, `CODEX_HOME`, `PATH`. Before pushing, run the suite with an empty HOME and a minimal PATH (CI has no Codex). A test run once wrote into the Codex plugin's data dir through an inherited variable.
- A checkout has no config location of its own: `route.py` refuses to run without `DELEGATE_CONFIG_DIR` (a default would split state from the installed plugin; task-loop hit this). Use a temp dir for experiments.

## Release

Tests green -> CI green on the exact HEAD (3 OS x Python 3.8 and 3.12) -> bump `.claude-plugin/plugin.json` -> same version in `iorlas-marketplace` -> tag `vX.Y.Z` -> `claude plugin marketplace update` and `claude plugin update iorlas-delegate@iorlas-marketplace`; sessions restart to load it.
