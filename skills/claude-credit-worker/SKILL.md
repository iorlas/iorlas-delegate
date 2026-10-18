---
name: claude-credit-worker
description: How the CLAUDE_CREDIT lane runs a Claude worker (Sonnet, Haiku or Opus) on the Anthropic API credit instead of the Max session and weekly limits, and how to set up its API key. Use when `delegate` returns a command step for CLAUDE_CREDIT, or when setting up or debugging that lane. Do not choose the model or effort yourself; the router (`route.py next`) does.
---

# claude-credit-worker

Lane `CLAUDE_CREDIT`: `claude -p` with an API key from the Console organization linked to the Max plan. The credit covers the Claude API and Agent SDK, not interactive Claude Code, so only this runner may use the key. The worker loads the project's CLAUDE.md, skills, plugins and MCP like a normal `claude` run in `--cwd`; only the login differs. A missing or bad key fails loudly and never falls back to the Max login.

## Run

The router prints the exact command in `d.how.argv`. Shape:

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/delegate/route.py run-claude --model opus-5.5 --effort medium --cap 3 [--write] [--allow TOOL] [--cwd DIR] [--bare] "<brief>"    # or the brief on stdin
```

- `--model` is a key from `routing.json` (`opus-5.5`, `sonnet-5.5`, `haiku-5.5`, `fable-5.1`). `--effort` low / medium / high. `--cap` is the dollar cap for this run.
- Read-only by default (Read, Glob, Grep). `--write` adds Edit, Write, Bash; `--allow "mcp__x__tool"` adds one more tool. `git push` and `git commit` are always denied.
- The worker is told it is a worker: no further delegation (`DELEGATE_DEPTH=1`), no commits, a short closing report.
- `--bare` strips CLAUDE.md, skills, plugins, MCP and hooks (hermetic worker).
- stdout = the answer. stderr last line = `cost=$X turns=N cycle=$Y/$CAP`.

## Spend limits

- Per run: `--cap`, passed to the CLI as `--max-budget-usd`.
- Per credit cycle: every run's cost is appended to `ledger.jsonl` in the config directory; a run is refused (exit 77) when the cycle's spend plus its cap would pass `credit.monthly_cap` (default $150). The cycle starts on `credit.reset_day` (default 1). Set both in `config.json`. The ledger counts only this runner's runs.
- The credit balance itself is not readable. When it is empty the API says so, the runner exits 75 and marks the lane out until the next reset day; the next successful run clears the mark.

## Exit codes

| code | meaning |
|---|---|
| 0 | done |
| 75 | credit exhausted; lane out until the next reset day |
| 76 | rate limited or overloaded; retry once after a minute |
| 77 | own cycle cap reached; lane out |
| 2 | API key not stored; tell the user the one-time setup |
| 1 | other failure; read stderr, do not retry blindly |

## Key setup (the user does this once, in their own terminal)

The key comes from a Console workspace linked to the Max plan (claude.ai → Settings → Billing → API credits), with a spend limit. Lookup order: environment variable `CLAUDE_CREDIT_WORKER_KEY` (not an `ANTHROPIC_*` name, so interactive Claude Code ignores it); the file `claude-credit.key` in the config directory (`~/.config/delegate`, `%APPDATA%\delegate`), readable only by the user; the legacy `~/.config/claude-credit-worker/key`; on macOS the keychain item `anthropic-worker-key` (`security add-generic-password -U -a "$USER" -s anthropic-worker-key -w`). Never export `ANTHROPIC_API_KEY` globally: interactive Claude Code would then bill the credit.

## Brief template

The worker has no memory of this session. Put in the brief: goal; files and paths; constraints; acceptance check (command or test that must pass); "no commits, no pushing, do not weaken tests". Name any project rule the task needs even though CLAUDE.md is loaded. Verify the result yourself.
