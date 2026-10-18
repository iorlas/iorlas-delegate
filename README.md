# iorlas-delegate

A Claude Code plugin that tells an orchestrating session how to hand sub-tasks to worker models, in a fixed order, and to check plan limits first.

## How it works

`skills/delegate/route.py` is a deterministic router (Python 3.8+, standard library only; macOS, Linux, Windows, Docker). The model asks it `route.py next ROLE` and gets back which lane, model and effort to run, and the command shape. The script reads plan limits and reset times itself where it can (Codex live from `codex app-server`; the Claude subscription from the status-line file or from what the model passes in; the API credit from its own ledger and an out marker), applies one pick function and the role chains, runs a second pass with relaxed gates before it stops, and records state under `~/.config/delegate` (`%APPDATA%\delegate` on Windows). The model provides only what the script cannot read, executes the answer, and reports refusals.

- `delegate` skill: the loop, the inputs the model supplies, and the rules a script cannot enforce.
- `codex-worker` skill: how to run the `OPENAI_SUBSCRIPTION` lane (Codex plugin).
- `claude-credit-worker` skill: the `CLAUDE_CREDIT` lane (`route.py run-claude`), spend caps and key setup.
- `routing.json`: lanes, models, roles and thresholds as data. Add a lane or role by editing it. Fable only when the task names it.

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

- Codex: asked live.
- Claude subscription: Claude Code gives plan limits only to the status line. Call `route.py capture-limits` from your status line (`printf '%s' "$input" | python3 route.py capture-limits`) to save them, or pass the desktop app's `get_usage` windows with `--claude-usage`.
- Credit: no balance reading; the router counts its own spend per cycle and marks the lane out on "credit balance too low".
- Settings (reset day, caps, `claude_weekly_reset`) go in `config.json` in the config directory.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Requirements

`claude`, `codex`, `python3`, Node 18.18+ (for the Codex plugin).
