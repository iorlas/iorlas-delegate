# mode: init (setup is `route.py doctor`)

Delegation never waits on setup. Sub-agent-only works with nothing installed; lanes you set up are an upgrade. Everything the user reads is `say` from the script; relay it verbatim.
Never handle or ask for the API key: env var, terminal `set-key`, or the one-shot page `set-key --web`.

```
route.py doctor                          what each lane needs, with the fix for each item. READY = nothing blocks
route.py doctor --lane L --next          first step for lane L as JSON
    who == "agent":  route.py doctor --fix <step.do>     # installs, Codex login, status line
                     waiting_for_user: show say (link and code); wait for "done"; run --next again
    who == "user":   show say verbatim; wait for "done"; run --next again
route.py doctor --decline L              hide a lane for good      (--undecline L)
route.py doctor --ack ID                 this finding needs nothing (--unack ID), e.g. volumes
```

New lanes show up once as `notice` in a `run` answer: tell the user; "no" = `doctor --decline NAME`.
No terminal (Claude RC, phone): `route.py set-key --web` in Bash `run_in_background`; show only its first line (the link). `--host tailscale` (or a private IP) for another device. Plain HTTP, loopback/private/tailnet only, one use, 5 min.
Optional: `doctor --fix statusline`; `route.py config set claude_weekly_reset "Sun 17:00"`; `--open-browser` with `--fix codex_login` on a desktop; `doctor --deep` also checks the stored key.
Container: mount `~/.claude` and `~/.codex` (or the whole home) as volumes.
Engines (`run --unattended`): Claude steps run `claude -p` on the login; plan limits are read only when a status-line or `status --claude-usage` reading exists, and no reading never stops a run.
