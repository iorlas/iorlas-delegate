# mode: claude-credit (lane CLAUDE_CREDIT)

`claude -p` with an API key from the Console organization linked to the Max plan. `route.py run` launches it (`run-claude` underneath); you never build the command. The credit covers the API and Agent SDK, not interactive Claude Code, so only this runner uses the key. A missing or bad key fails loudly, never falls back to the Max login.

- Tools: Read Glob Grep; `--write` adds Edit Write Bash. Every git write command is denied and git state is compared before and after (exit 78).
- stdout = answer; stderr: `session=<id>` first (transcript `~/.claude/projects/*/<id>.jsonl`), cost and cycle spend last.

| exit | meaning | do |
|---|---|---|
| 0 | done | judge it |
| 75 | credit empty, lane out until the reset day | rerun `run` |
| 76 | rate limited | retry once after a minute, then rerun `run` |
| 77 | own cycle cap reached, lane out | rerun `run`; the user may raise `credit.monthly_cap` |
| 78 | worker changed git; answer still printed | `git reflog`; you decide; never auto-revert |
| 2 | no API key | read `modes/init.md`; never ask for it in chat |
| 1 | other | read stderr; no blind retry |

Spend: `--cap` per run; every run is appended to `ledger.jsonl`; a run is refused (77) when the cycle's spend plus its cap passes `credit.monthly_cap` (default 150; cycle starts on `credit.reset_day`, default 1). The balance itself is unreadable.
Key lookup: env `CLAUDE_CREDIT_WORKER_KEY` (not an `ANTHROPIC_*` name), else `claude-credit.key` in the config dir, else `~/.config/claude-credit-worker/key`, else macOS keychain `anthropic-worker-key`. The user stores it (`set-key`, `set-key --web`). Never export `ANTHROPIC_API_KEY` globally: interactive Claude Code would bill the credit.
