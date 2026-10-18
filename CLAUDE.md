# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Constitution (Denis's standing rules for this repo)

1. **No duplication, no redundancy.** One way to do each thing; one place for each fact and each copy of data (never two copies of a key or setting).
2. **No backward compatibility** (no shims, aliases, deprecated flags, migrations, old-format readers) **until Denis says the plugin has been given to someone else.** Public code does not change this. When a thing changes, change its callers and delete the old form; fix his own machine by hand.
3. **Small and simple.** Less code, fewer concepts, fewer commands. Prefer deleting to adding.
4. **Instructions are strict, terse and constrained.** Everything the orchestrator reads (SKILL.md, modes, script output) costs tokens in every session; the budgets are enforced by `tests/test_route.py` (`TokenBudget`).
5. **The script decides, the model supplies only what the script cannot read.** Portability lives in the script, not in the markdown.
6. **Workers never touch git;** the coordinator owns branches, worktrees, commits, merges.
7. Before changing how workers launch or limits are computed, read `docs/dev/integrations.md`.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

```bash
python3 -m unittest discover -s tests
```

CI: Ubuntu, macOS, Windows x Python 3.8 and 3.12.

## Architecture Overview

README.md (Layout). The skill (`skills/delegate/SKILL.md`) is the only file read on a normal call; `route.py` decides.

## Before changing how workers launch, how limits are computed, or what state is kept

Read `docs/dev/integrations.md`: other tools (the iorlas-task-loop dashboard) rely on the agent-runs v1 records
`skills/delegate/agent_runs.py` writes, and on nothing else. It says what a rewrite must keep and what it may drop.

## Conventions & Patterns

- Python 3.8+, standard library only; macOS, Linux (also in containers), Windows.
- Bump `.claude-plugin/plugin.json` and the iorlas-marketplace entry on every release.
