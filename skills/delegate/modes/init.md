# mode: init (setup is `route.py doctor`, also `route.py init`)

Delegation never waits on setup. Sub-agent-only works with nothing installed; lanes you set up are an upgrade. Relay the table as is.

```
route.py doctor            one row per lane: enabled, ready, fix (what to run). --json for the same as data
```

The fix column is the whole wizard: run what it says yourself (the Codex login prints a link and a code for the user to open), then run `doctor` again.
New lanes show up once as `notice` in a `run` answer: tell the user.
Optional: `route.py config set claude_weekly_reset "Sun 17:00"`; wire `route.py capture-limits --print` into the Claude status line so interactive sessions record plan limits.
Container: mount `~/.claude` and `~/.codex` (or the whole home) as volumes.
Engines (`run --unattended`): Claude steps run `claude -p` on the login; plan limits are read only when a status-line or `status --claude-usage` reading exists, and no reading never stops a run.
