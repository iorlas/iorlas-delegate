# mode: init

Goal: every lane READY, or opted out. Everything the user reads comes from `route.py init` as `say`; relay it verbatim, do not paraphrase commands.
Never handle the API key and never ask for it in chat: the user runs `route.py set-key` in their own terminal (hidden prompt).

```
loop:
    out = route.py init                                  # JSON: the first failing required step, or step == null (READY)
    if out.step is null:   say "delegate is ready"; show out.lanes; mention out.optional_steps once; stop
    if out.who == "agent":
        r = route.py init --do <out.do>                  # installs the Codex CLI / plugin, starts the Codex login, wires the status line
        if r.waiting_for_user:   show r.say verbatim (link and code); wait for the user to say done
        else:                    relay r.say
    if out.who == "user":      show out.say verbatim (and out.url); wait for the user to say done
    goto loop
user declines a lane:       route.py init --decline <LANE>          (undo: --undecline)
optional, only if asked:    route.py init --do statusline          ;   route.py config set claude_weekly_reset "Sun 17:00"
desktop with a browser:     route.py init --do codex_login --open-browser
finish:                     route.py doctor --deep               # also checks the stored API key against the API (free)
```

Ask the user only: when the Claude weekly limit resets (day and local time), and which lanes to opt out of.
Container: mount `~/.codex` and `~/.config/delegate` as volumes, or logins and state vanish on rebuild.
Headless limit: plan limits reach only the status line, which does not run under `-p`, Claude RC or containers; there the Claude subscription's use stays unknown and Claude steps prefer the credit lane.
