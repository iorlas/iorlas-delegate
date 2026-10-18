# mode: claude-credit (lane CLAUDE_CREDIT)

`claude -p` with an API key from the Console organization linked to the Max plan. The credit covers the API and Agent SDK, not interactive Claude Code, so only this runner may use the key. The worker loads the project's CLAUDE.md, skills, plugins and MCP like a normal `claude` run in `--cwd`; only the login differs. A missing or bad key fails loudly and never falls back to the Max login.

```
run: d.how.argv, brief on stdin (Bash run_in_background for long tasks)
     python3 route.py run-claude --model <key from routing.json> --effort low|medium|high --cap <usd> [--write] [--allow TOOL] [--cwd DIR] [--bare]
default tools: Read Glob Grep.   --write adds Edit Write Bash.   --allow adds one tool.   git push / git commit always denied.
--bare strips CLAUDE.md, skills, plugins, MCP, hooks.   The worker is told: no delegation (DELEGATE_DEPTH=1), no commits, short closing report.
stdout = answer;  stderr last line = cost, turns, cycle spend / cap.
```

| exit | meaning | do |
|---|---|---|
| 0 | done | judge the result |
| 75 | credit empty; lane marked out until the next reset day | `route.py next` again |
| 76 | rate limited | retry once after a minute, then `route.py next` |
| 77 | own cycle cap reached; lane out | `route.py next` again; the user may raise `credit.monthly_cap` |
| 2 | no API key | `route.py init`; never ask for the key in chat |
| 1 | other | read stderr; do not retry blindly |

Spend limits: `--cap` per run (`--max-budget-usd`); per cycle every run is appended to `ledger.jsonl` and a run is refused (exit 77) when the cycle's spend plus its cap would pass `credit.monthly_cap` (default 150), the cycle starting on `credit.reset_day` (default 1). The ledger counts only this runner. The balance itself is not readable.

Key: env `CLAUDE_CREDIT_WORKER_KEY` (not an `ANTHROPIC_*` name), else `claude-credit.key` in the config directory, else the legacy `~/.config/claude-credit-worker/key`, else on macOS the keychain item `anthropic-worker-key`. The USER stores it with `route.py set-key` in their own terminal. Never export `ANTHROPIC_API_KEY` globally: interactive Claude Code would then bill the credit.
