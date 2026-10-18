# iorlas-delegate

A Claude Code plugin that tells an orchestrating session how to hand sub-tasks to worker models, in a fixed order, and to check plan limits first.

## Skills

- `delegate`: strategy. Three execution lanes (`codex`, `max`, `credit`), a glossary, a function that picks between the two Claude lanes, and one ordered chain per role (planner, worker, reviewer, hard, light) that lists every lane and ends in STOP, a rule for choosing between Max and the API credit from live limits and reset times, and a limits check (`limits.py`). Fable only when the task names it.
- `codex-worker`: the `codex` lane. Codex (GPT-6.1 Sol on ChatGPT Plus) through the official `codex@openai-codex` plugin, and how to tell the allowance is used up.
- `claude-credit-worker`: the `credit` lane. `claude -p` on an Anthropic API key (the Max plan's monthly API credit), so it never draws on the Max limits. It sees the project's CLAUDE.md, skills, plugins and MCP like a normal run; profiles (`worker`, `reviewer`, `cheap`, `hard` = Opus low, `very-hard` = Opus medium, `impossible` = Fable medium) set model, effort and per-run cap; a per-cycle cap (`MONTHLY_CAP`, default $150) and the reset day (`CREDIT_RESET_DAY`) guard the credit. The `max` lane is the native subagent on Max.

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

The key comes from a Console workspace linked to the Max plan (claude.ai, Settings, Billing, API credits), with a spend limit. Key lookup order: env `CLAUDE_CREDIT_WORKER_KEY` (not an `ANTHROPIC_*` name, so interactive Claude Code ignores it), then the file `~/.config/claude-credit-worker/key` (mode 600; Linux), then the macOS keychain.

## Limit awareness

`skills/delegate/limits.py` prints use, reset time and a verdict (OK, BURN, OUT, STALE, UNKNOWN) per lane.

- Codex: asked live from `codex app-server` (`account/rateLimits/read`).
- Max: Claude Code gives plan limits only to the status line. Call `skills/delegate/capture-limits.sh` from your status line command (`printf '%s' "$input" | bash capture-limits.sh`); it writes `~/.claude/state/max-limits.json`. Without it Max is UNKNOWN. Only an interactive session keeps the file fresh.
- Credit: no balance reading. It shows the reset day, this script's own spend and an out marker that `run.sh` writes on exit 75.
- Config file `~/.config/claude-credit-worker/config`: `MONTHLY_CAP`, `CREDIT_RESET_DAY`, `MAX_WEEKLY_RESET="Sun 17:00"` (the weekly reset moment, used when the status-line file is missing).
- Desktop app: `mcp__ccd_session_mgmt__get_usage` gives the Max windows directly; `delegate` prefers it there.

## Requirements

`claude`, `codex`, `jq`, `python3`, Node 18.18+ (for the Codex plugin).
