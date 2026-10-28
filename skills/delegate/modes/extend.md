# mode: extend

Data is in `routing.json`; code is in `src/delegate` (the launcher `route.py` only starts it): `app/usage.py` (readers), `app/decide.py` (runners), `app/onboard.py` (readiness checks), `app/setup.py` (fixers).

```
add a model:   routing.json models: {"<key>": {"family": F, "api": "<id>", "agent": "<alias>"}}
add a role:    routing.json roles: {"<name>": {advance: on_unavailable|on_failure, steps: [{model, effort, on: <family>, mode, cap}], stop: "<text>"}}
               optional: by_author {<family>: [steps]}, degraded_needs_consent [steps]
add a lane:    routing.json lanes: {"<FAMILY_KIND>": {family, kind: subscription|credit, reader, runner, ready, shared_with_orchestrator?}}
               reader  -> a function in app/usage.py READERS that fills the Lane (use, resets, out)
               runner  -> a branch in app/decide.py how() that returns the command shape
               ready   -> a key in app/onboard.py READY_CHECKS; add a check there and a fixer in app/setup.py if it needs setup
per-user settings:  route.py config set <dotted.key> <json>      (thresholds, caps, claude_weekly_reset, credit.reset_day)
```

Chains name families, not lanes, so a new lane of an existing family needs no chain change. Tests: `python3 -m unittest discover -s tests` in the plugin root.
