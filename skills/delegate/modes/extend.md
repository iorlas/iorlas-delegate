# mode: extend

Data is in `routing.json`; code is in `route.py` (decisions, readers, runners) and `onboard.py` (readiness checks, fixers).

```
add a model:   routing.json models: {"<key>": {"family": F, "api": "<id>", "agent": "<alias>"}}
add a role:    routing.json roles: {"<name>": {advance: on_unavailable|on_failure, steps: [{model, effort, on: <family>, mode, cap, use}], stop: "<text>"}}
               optional: by_author {<family>: [steps]}, degraded_needs_consent [steps]
add a lane:    routing.json lanes: {"<FAMILY_KIND>": {family, kind: subscription|credit, reader, runner, ready, shared_with_orchestrator?}}
               reader  -> a function in route.py READERS that fills the Lane (use, resets, out)
               runner  -> a branch in route.py how() that returns the command shape
               ready   -> a key in onboard.py READY_CHECKS; add a check and a fixer to onboard.py if it needs setup
per-user settings:  route.py config set <dotted.key> <json>      (thresholds, caps, claude_weekly_reset, credit.reset_day)
```

Chains name families, not lanes, so a new lane of an existing family needs no chain change. Tests: `python3 -m unittest discover -s tests` in the plugin root.
