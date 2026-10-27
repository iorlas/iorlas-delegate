---
description: Lane cockpit: every delegate lane at a glance (state emoji, reason, unlock time, usage windows, model, next worker).
---
Run the `delegate` skill's `route.py status` with this machine's Python 3 (`python3`) and show its output as is: it is already the cockpit, one row per known lane (configured or not). Emoji: ✅ ready, ⏳ resting on a usage limit, 🔑 logged out, 💸 spending or credit limit, 🐢 slow or hung recently, ⛔ disabled or not configured, ❓ no read yet. Locked lanes carry their unlock time; usage windows carry their source and age. `route.py status --json` has the same fields under `cockpit` for dashboards. If a lane is 🔑 or ⛔, run `route.py doctor` and relay what it says to do.
