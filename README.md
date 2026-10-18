# iorlas-delegate

A Claude Code plugin that tells an orchestrating session how to hand sub-tasks to worker models, in a fixed order, and to check plan limits first.

## Skills

- `delegate`: strategy. Lane order, what never gets delegated, the hard-task rule (Opus on short split subtasks), and a limits check (`limits.py`) before each sub-task. A weekly window that is about to reset gets spent first.
- `codex-worker`: lane 1. Codex (GPT-6.1 Sol on ChatGPT Plus) through the official `codex@openai-codex` plugin, and how to tell the allowance is used up.
- `claude-credit-worker`: lane 2. `claude --bare -p` on an Anthropic API key (the Max plan's monthly API credit), so it never draws on the Max limits. Lane 3 is the native Sonnet subagent on Max.

## Install

```sh
claude plugin marketplace add iorlas/iorlas-marketplace
claude plugin install iorlas-delegate@iorlas-marketplace
claude plugin marketplace add openai/codex-plugin-cc
claude plugin install codex@openai-codex
codex login
```

Store the worker key once, in your own terminal (the `-w` flag prompts, so it stays out of shell history):

```sh
security add-generic-password -U -a "$USER" -s anthropic-worker-key -w
```

The key comes from a Console workspace linked to the Max plan (claude.ai, Settings, Billing, API credits), with a spend limit. macOS keychain only; on Linux set `CLAUDE_CREDIT_WORKER_KEY` in the worker's own environment, never globally.

## Limit awareness

`skills/delegate/limits.py` prints use, reset time and a verdict (OK, BURN, OUT, STALE, UNKNOWN) per lane.

- Codex: asked live from `codex app-server` (`account/rateLimits/read`).
- Max: Claude Code gives plan limits only to the status line. Call `skills/delegate/capture-limits.sh` from your status line command (`printf '%s' "$input" | bash capture-limits.sh`); it writes `~/.claude/state/max-limits.json`. Without it Max is UNKNOWN. Only an interactive session keeps the file fresh.
- Credit: not readable; `run.sh` exits 75 when empty.

## Requirements

`claude`, `codex`, `jq`, `python3`, Node 18.18+ (for the Codex plugin).
