---
name: claude-credit-worker
description: Run one self-contained coding or research task as a Claude worker (Sonnet, Haiku or Opus) billed to the Anthropic API credit, not to the Max session/weekly limits. The worker sees the project's CLAUDE.md, skills, plugins and MCP like a normal `claude` run. Use when `delegate` reaches lane 2, or when a task should go to Claude without draining Max.
---

# claude-credit-worker

Runs `claude -p` with an API key from the Console org linked to the Max plan. The worker loads the same CLAUDE.md, skills, plugins, MCP servers, hooks and settings as a normal `claude` run in `--cwd`. Only the login differs: a missing or bad key fails loudly, it never falls back to the Max login.

## Run

```bash
${CLAUDE_SKILL_DIR}/run.sh [--profile NAME] [--write] [--allow TOOL] [--model ID] [--effort LEVEL] [--budget USD] [--cwd DIR] [--bare] "<brief>"
${CLAUDE_SKILL_DIR}/run.sh --status        # this script's own spend this month vs its cap
```

## Pick a profile

| profile | model | effort | cap per run | use for |
|---|---|---|---|---|
| `worker` (default) | Sonnet 5.5 | medium | $2 | implementation, debugging, tests |
| `light` | Sonnet 5.5 | low | $1 | small, clear edits and simple checks where a miss is cheap |
| `reviewer` | Opus 5.5 | medium | $2 | verification and code review: rerun the checks, read the diff (read-only). Opus 5.5 scored a little above Sonnet 5.5 on Terminal-Bench 4.0 at a lower cost per test, and Sonnet's scores drop sharply below max effort. No code-review benchmark exists: measure on your own diffs |
| `cheap` | Haiku 5.5 | low | $0.25 | routing, classification, bulk reading, short summaries; keep prompts under 100K tokens |
| `hard` | Opus 5.5 | low | $3 | one short, split subtask of a hard ticket (see `delegate`); never the whole ticket |
| `very-hard` | Opus 5.5 | medium | $5 | a subtask that failed at `hard` |
| `impossible` | Fable 5.1 | medium | $6 | a subtask that failed twice at `very-hard`; Fable costs about 2.5x Opus per token |

Flags override the profile. Raise effort only for a subtask that failed at the profile's level.

## Behaviour

- Read-only by default (Read, Glob, Grep). `--write` adds Edit, Write, Bash; `--allow "mcp__x__tool"` adds one more tool (repeatable). `git push` and `git commit` are always denied.
- The worker is told it is a worker: no further delegation (`DELEGATE_DEPTH=1` is set; if you are running under it, do not call `delegate`), no commits, and a short closing report with the exit code and last lines of any check it ran.
- `--bare` strips CLAUDE.md, skills, plugins, MCP and hooks (hermetic worker); use it only when the project's own setup would get in the way.
- Long task: run with Bash `run_in_background`, then read the output when it ends.
- Parallel `--write` workers need separate git worktrees (`--cwd`).
- stdout = the answer. stderr last line = `cost=$X turns=N month=$Y/$CAP`.

## Spend limits

- Per run: `--max-budget-usd` from the profile.
- Per credit cycle: the script logs each run's cost to `~/.config/claude-credit-worker/spend.jsonl` and refuses (exit 77) when the cycle's spend plus the run's cap would pass `MONTHLY_CAP` (default $150). The cycle starts on `CREDIT_RESET_DAY` (default 1; set both in `~/.config/claude-credit-worker/config`). The ledger counts only this script's runs; other uses of the same key are not counted.
- The credit balance itself is not readable. When it is empty the API says so, the script exits 75 and writes an out marker (`~/.config/claude-credit-worker/out-until`, set to the next reset day) that `delegate`'s `limits.py` reads; the next successful run clears it.

## Exit codes

| code | meaning | next |
|---|---|---|
| 0 | done | review the output |
| 75 | credit exhausted | lane is out until the next reset day (`run.sh --status` shows it); fall to the next lane and tell the user |
| 76 | rate limited or overloaded | retry once after a minute, then next lane |
| 77 | own monthly cap reached | lane is out; tell the user, who may raise `MONTHLY_CAP` |
| 2 | API key not stored | tell the user the one-time command printed on stderr |
| 1 | other failure | read stderr; do not retry blindly |

## Key setup (user does this once, in their own terminal)

macOS: `security add-generic-password -U -a "$USER" -s anthropic-worker-key -w`
Linux: write the key to `~/.config/claude-credit-worker/key` (mode 600) or set `CLAUDE_CREDIT_WORKER_KEY` in the worker's own environment.

The key must come from a Console workspace linked to the Max plan (claude.ai → Settings → Billing → API credits), with a spend limit set. Never export `ANTHROPIC_API_KEY` globally: interactive Claude Code would then bill the credit, which does not cover it.

## Brief template

The worker has no memory of this session. Put in the brief: goal; files and paths; constraints; acceptance check (command or test that must pass); "no commits, no pushing, do not weaken tests". Name any project rule the task needs even though CLAUDE.md is loaded. Verify the result yourself (rerun the check; read the diff on a risk flag); do not trust the worker's report.
