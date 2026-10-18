---
name: claude-credit-worker
description: Run one self-contained coding or research task as a Sonnet worker on the Anthropic API credit (the $200/mo Max credit), not on the Max session/weekly limits. Use when `delegate` reaches lane 2, or when a task should go to Claude (Sonnet, Haiku or Opus) without draining Max.
---

# claude-credit-worker

Runs `claude --bare -p` with an API key from the linked Console org. `--bare` means only that key can authenticate: a missing or bad key fails loudly, it never falls back to the Max login.

## Run

```bash
${CLAUDE_SKILL_DIR}/run.sh [--write] [--model ID] [--effort LEVEL] [--budget USD] [--cwd DIR] "<brief>"
```

- Default is read-only (Read, Glob, Grep). `--write` adds Edit, Write, Bash; `git push` and `git commit` are always denied.
- `--effort low|medium|high|xhigh|max` sets reasoning effort; Opus runs use `claude-opus-5-5 --effort low` on short, split subtasks only (see `delegate`).
- `--budget` caps one run (default $2). `--model` default `claude-sonnet-5-5`; use `claude-haiku-5-5` for routing, classification, short reviews.
- Long task: run with Bash `run_in_background`, then read the output when it ends.
- Parallel `--write` workers need separate git worktrees (`--cwd`).
- stdout = the answer. stderr last line = `cost=$X turns=N`.

## Exit codes

| code | meaning | next |
|---|---|---|
| 0 | done | review the output |
| 75 | credit exhausted | lane is out until the billing cycle resets; fall to the next lane |
| 76 | rate limited or overloaded | retry once after a minute, then next lane |
| 2 | API key not stored | tell the user the one-time command printed on stderr |
| 1 | other failure | read stderr; do not retry blindly |

## Key setup (user does this once, in their own terminal)

```bash
security add-generic-password -U -a "$USER" -s anthropic-worker-key -w
```

The key must come from a Console workspace linked to the Max plan (claude.ai → Settings → Billing → API credits), with a spend limit set. Never export `ANTHROPIC_API_KEY` globally: it would make interactive Claude Code bill the credit, which does not cover it.

## Brief template

The worker has no memory of this session and `--bare` skips CLAUDE.md. Put in the brief: goal; files and paths; constraints; acceptance check (command or test that must pass); "no commits, no pushing, do not weaken tests". Verify the result yourself (diff, run the check); do not trust the worker's report.
