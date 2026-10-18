# mode: codex (lane OPENAI_SUBSCRIPTION)

GPT-6.1 Sol and GPT-6 Luna on the ChatGPT Plus plan, through the Codex CLI and the official plugin `codex@openai-codex`. Not Max, not the API credit. Defaults live in `~/.codex/config.toml` (Sol, medium).

```
d.use == "task"                          # implement / debug, write access
    run: node <plugin cache>/openai-codex/codex/<ver>/scripts/codex-companion.mjs task --write [--resume-last] "<brief>"
         inside a Bash call with run_in_background; the harness says when it ends.
    NOT /codex:rescue --background: such jobs stayed queued forever with no worker (lifesim, 2026-10-14).
    /codex:rescue (foreground) is fine for a short task; its subagent wrapper runs on Sonnet and uses a little Max quota.
    leave --model and --effort unset unless d.effort differs from the config; then --effort <d.effort>.
d.use == "review"                        # diff written by Claude, read-only
    /codex:review   (or /codex:adversarial-review "<focus>")
d.use == "adversarial_review_of_plan"
    /codex:adversarial-review on the draft plan file; or the companion with a planning brief that forbids edits.
d.use == "read_only_exec"                # GPT-6 Luna: reading and summarising only, never edits or judgement
    codex exec -s read-only -m gpt-6-luna -c model_reasoning_effort=low "<brief>"
```

- Continue earlier Codex work: `--resume-last` (threads are per workspace, so parallel worktrees never pick up each other's). Unrelated task: fresh.
- Parallel Codex writers each need their own directory, made by the coordinator; one directory takes one writer. Codex never creates worktrees or branches here: `codex exec -C <cwd>` pins it to the directory the router gives; run `d.how.guard` around it.
- Luna has a very large Plus allowance (350-3,000 messages per 5 hours on OpenAI's page) and is weak (13.6% on Terminal-Bench 4.0).
- `codex exec` waits forever on an open stdin pipe (it prints "Reading additional input from stdin..."; reproduced 2026-10-18). Give it the brief as an argument and close stdin (`< /dev/null`; Windows `< NUL`), or pipe the brief in. To stop a stuck one, kill its process group: killing only `codex` leaves the vendor binary running.
- Never trust Codex's summary: read the diff, rerun the acceptance check.

## When Codex refuses

```
if the result is empty or an error:
    probe: codex exec --skip-git-repo-check "Reply with exactly: ok"          # from any directory
    if it prints ok:   the failure was the task or the plugin; retry once fresh, then report.
    if it names a usage / rate limit or "try again later":
        route.py report --lane OPENAI_SUBSCRIPTION --event out --message "<its text>"     # the router reads the real reset time itself
        tell the user in one line; call route.py next again
    if codex is not signed in:   route.py init
```

Not yet seen in practice: the exact wording of Codex's usage-limit message. When it first appears, add it here.
