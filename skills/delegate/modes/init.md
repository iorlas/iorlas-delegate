# mode: init

Goal: every lane READY, or declined. Everything the user reads is `say` from `route.py init`; relay it verbatim, never paraphrase a command.
Never handle or ask for the API key. The key comes by env var, terminal `set-key`, or the one-shot page `set-key --web`.

```
loop:
    out = route.py init                       # JSON: first failing required step, or step == null (READY)
    step null:        say "delegate is ready"; show out.lanes; mention out.optional_steps once; stop
    who == "agent":   r = route.py init --do <out.do>      # Codex CLI / plugin install, Codex login, status line
                      r.waiting_for_user: show r.say (link and code); wait for "done"
    who == "user":    show out.say verbatim; wait for "done"
decline a lane:      route.py init --decline <LANE>      (undo: --undecline)
needs nothing:       route.py config ack <ID>            (undo: unack)   e.g. volumes, when the home folder is already a volume
```

No terminal (Claude RC, phone): run `route.py set-key --web` in Bash `run_in_background`, read its first output line, show only that link. Add `--host tailscale` (or a private IP) when the user is on another device. Plain HTTP: loopback, private and tailnet addresses only; one use; closes after 5 min.
Optional, only if asked: `route.py init --do statusline`; `route.py config set claude_weekly_reset "Sun 17:00"`; browser on this machine: `init --do codex_login --open-browser`; finish with `route.py doctor --deep`.
Container: mount `~/.codex` and `~/.config/delegate` (or the whole home) as volumes, or logins and state vanish on rebuild.
Headless (`-p`, RC, containers): plan limits reach only the status line, which does not run there. Claude steps then prefer the credit lane.
