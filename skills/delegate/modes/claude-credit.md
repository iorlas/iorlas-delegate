# mode: claude-credit (lane CLAUDE_CREDIT)

`claude -p` with an API key from the Console organization linked to the Max plan. `route.py run` launches it; you never build the command. Headless callers (no Claude session behind the process) never get this lane: CLAUDE_SUBSCRIPTION runs `claude -p` on the logged-in account instead, with no API key and no dollar cap. The credit covers the API and Agent SDK, not interactive Claude Code, so only this runner uses the key. A missing or bad key fails loudly, never falls back to the Max login.

- Tools: Read Glob Grep; `--write` adds Edit Write Bash. Every git write command is denied; HEAD and branch are compared before and after (`git_violation`).
- The worker's Claude session id is `ref`; its transcript is `~/.claude/projects/*/<ref>.jsonl`.

| `status` | meaning | do |
|---|---|---|
| `done` | answer in `text`; `ref` is the session id (`--resume REF` continues it) | judge it |
| `lane_out` | credit empty, rate limited, or the cycle cap reached; `retry_after` says when | rerun `run`; the user may raise `credit.monthly_cap` |
| `timeout` | killed after `--timeout` | shrink the task |
| `error` | no API key, or `claude` missing | read `modes/init.md`; never ask for a key in chat |
| `failed` | anything else; `say` has the tail of stderr | no blind retry |

Spend: `--cap` per run; every run is appended to `ledger.jsonl`; a run is refused (77) when the cycle's spend plus its cap passes `credit.monthly_cap` (default 150; cycle starts on `credit.reset_day`, default 1). The balance itself is unreadable.
Key lookup: env `CLAUDE_CREDIT_WORKER_KEY` (not an `ANTHROPIC_*` name), else `claude-credit.key` in the config dir, else macOS keychain `anthropic-worker-key`. The user stores it (`set-key`, `set-key --web`). Never export `ANTHROPIC_API_KEY` globally: interactive Claude Code would bill the credit.
