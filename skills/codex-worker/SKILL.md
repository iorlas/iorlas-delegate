---
name: codex-worker
description: Hand one self-contained coding task to Codex (GPT-6.1 Sol on the ChatGPT Plus plan) through the official codex plugin, and detect when the plan allowance is used up. Use when `delegate` reaches lane 1.
---

# codex-worker

Codex runs GPT-6.1 Sol on the user's ChatGPT Plus plan (not Max, not the API credit). Plugin: `codex@openai-codex` (openai/codex-plugin-cc). User config: `~/.codex/config.toml` (model `gpt-6.1-sol`, effort `medium`).

## Run

- Implementation or debugging task: the `codex:codex-rescue` subagent, or `/codex:rescue [--background|--wait] [--resume|--fresh] [--effort LEVEL] <brief>`. It writes files by default.
- Leave `--model` and `--effort` unset: the config gives Sol at medium. Raise to `--effort high` only for a subtask that failed twice at medium.
- Long or multi-step task: not `--background` (in lifesim such jobs stayed `queued` forever with no worker, 2026-10-14). Run the companion in the foreground inside a Bash call with `run_in_background`: `node ~/.claude/plugins/cache/openai-codex/codex/<ver>/scripts/codex-companion.mjs task --write [--resume-last] "<brief>"`; the harness says when it ends. This also skips the `codex-rescue` wrapper and its Max quota.
- Review of a finished diff, read-only: `/codex:review` or `/codex:adversarial-review` (different model family from Claude, good for a second opinion).
- Continue earlier Codex work: `--resume` (`--resume-last` with the companion; threads are per workspace, so parallel worktrees never pick up each other's). Unrelated task: `--fresh`.
- Parallel Codex writers need separate git worktrees; one working directory takes one writer.
- `codex-rescue` is a thin wrapper that runs on Sonnet; it uses a little Max quota per call, and it forwards the brief as written.

## Brief template

Goal; files and paths; constraints; acceptance check (a command or test that must pass); "no commits, no pushing, do not weaken tests". Codex does not see this session.

## Detecting an exhausted allowance

The plugin has no usage-limit handling of its own, and the wrapper is told to return nothing when Codex cannot be invoked. So an empty or error result from `codex-rescue` is not yet proof of exhaustion:

1. Probe once, cheaply: `codex exec --skip-git-repo-check "Reply with exactly: ok"` from any directory.
2. Output `ok` means Codex works; the failure was in the task or the plugin. Retry once with `--fresh`, then report.
3. A usage-limit, rate-limit or "try again later" message means the plan allowance is used up (5-hour window or weekly cap). Tell the user once, in one line, quote the message, and treat lane 1 as out until its reset time (the message usually says when); then probe again (step 1) and come back (`delegate`, rule 1).
4. `codex login status` not "Logged in using ChatGPT": tell the user to run `codex login`.

Not yet seen in practice: the exact wording of the usage-limit message. When it first appears, add it here.

## Never

Do not trust Codex's own summary of success: read the diff and run the acceptance check.
