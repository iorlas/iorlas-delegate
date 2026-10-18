#!/usr/bin/env bash
# Save the Max plan's 5-hour and weekly limits so `limits.py` can read them.
# Claude Code hands plan limits only to the status line, so call this from it:
#   input=$(cat); printf '%s' "$input" | bash /path/to/capture-limits.sh
# Writes ~/.claude/state/max-limits.json only when the JSON carries rate_limits (Pro/Max, after the first reply).
set -u
input="$(cat)"
printf '%s' "$input" | jq -e '.rate_limits' >/dev/null 2>&1 || exit 0
dir="$HOME/.claude/state"
mkdir -p "$dir"
tmp="$dir/max-limits.json.$$"
printf '%s' "$input" | jq -c '{updated: now, rate_limits: .rate_limits}' > "$tmp" && mv "$tmp" "$dir/max-limits.json"
