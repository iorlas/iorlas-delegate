#!/usr/bin/env bash
# Run one task with `claude -p` on the Anthropic API credit, never on the Max login.
#
# usage: run.sh [--profile NAME] [--write] [--allow TOOL]... [--model ID] [--effort LEVEL]
#               [--budget USD] [--cwd DIR] [--bare] [--] "prompt"      (or prompt on stdin)
#        run.sh --status
#
# profiles (flags override them):   model              effort  per-run cap
#   worker    (default)             claude-sonnet-5-5  medium  $2
#   light                           claude-sonnet-5-5  low     $1
#   reviewer                        claude-opus-5-5    medium  $2
#   planner                         claude-opus-5-5    medium  $3
#   cheap                           claude-haiku-5-5   low     $0.25
#   hard                            claude-opus-5-5    low     $3
#   very-hard                       claude-opus-5-5    medium  $5
#   impossible                      claude-fable-5-1   medium  $6
#
# Environment: by default the worker sees what a normal `claude` run sees in --cwd (CLAUDE.md, skills, plugins,
# MCP servers, hooks, settings). Only the login differs: ANTHROPIC_API_KEY is set, and a bad key fails with
# "Invalid API key" instead of falling back to the Max login. --bare strips all of that (hermetic worker).
#
# stdout: the worker's final answer.  stderr: last line "cost=$X turns=N month=$Y/$CAP".
# exit:   0 ok | 75 credit exhausted | 76 rate limited or overloaded | 77 own monthly cap reached
#         2 key missing | 1 other failure
#
# Config (optional): ~/.config/claude-credit-worker/config, shell syntax:
#   MONTHLY_CAP=150        dollars per credit cycle this script may spend (the credit is $200 on Max 20x)
#   CREDIT_RESET_DAY=1     day of the month the credit resets (billing cadence)
set -uo pipefail

CONF_DIR="$HOME/.config/claude-credit-worker"
KEY_SERVICE="anthropic-worker-key"
LEDGER="$CONF_DIR/spend.jsonl"
MONTHLY_CAP="150"   # dollars; the credit is $200/mo on Max 20x, kept below it as a margin
CREDIT_RESET_DAY="1"
OUT_MARK="$CONF_DIR/out-until"
[ -r "$CONF_DIR/config" ] && . "$CONF_DIR/config"

# start of the current credit cycle, as YYYY-MM-DD (CREDIT_RESET_DAY of this or last month)
cycle_start() {
  python3 - "$CREDIT_RESET_DAY" <<'PY'
import sys, datetime as d
day = min(int(sys.argv[1] or 1), 28)
t = d.date.today()
s = t.replace(day=day)
if s > t:
    s = (t.replace(day=1) - d.timedelta(days=1)).replace(day=day)
print(s.isoformat())
PY
}
next_reset() {
  python3 - "$CREDIT_RESET_DAY" <<'PY'
import sys, datetime as d
day = min(int(sys.argv[1] or 1), 28)
t = d.date.today()
s = t.replace(day=day)
if s <= t:
    s = ((t.replace(day=1) + d.timedelta(days=32)).replace(day=1)).replace(day=day)
print(s.isoformat())
PY
}

month_spend() {
  [ -r "$LEDGER" ] || { echo 0; return; }
  jq -s --arg c "$(cycle_start)" '[.[] | select(.ts >= $c) | .cost] | add // 0' "$LEDGER" 2>/dev/null || echo 0
}

if [ "${1:-}" = "--status" ]; then
  echo "cycle since $(cycle_start): \$$(month_spend) spent by this script of \$$MONTHLY_CAP cap; credit resets $(next_reset) (own ledger; the credit balance itself is not readable)"
  [ -r "$OUT_MARK" ] && echo "out marker set: a run saw 'credit balance too low' (cleared by the next successful run, or: rm $OUT_MARK)"
  exit 0
fi

profile="worker"; model=""; effort=""; budget=""; cwd="$PWD"; write=0; bare=0; allow=()
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --write)   write=1; shift ;;
    --allow)   allow+=("$2"); shift 2 ;;
    --model)   model="$2"; shift 2 ;;
    --effort)  effort="$2"; shift 2 ;;
    --budget)  budget="$2"; shift 2 ;;
    --cwd)     cwd="$2"; shift 2 ;;
    --bare)    bare=1; shift ;;
    --)        shift; break ;;
    *)         break ;;
  esac
done

case "$profile" in
  worker)   p_model="claude-sonnet-5-5"; p_effort="medium"; p_budget="2" ;;
  light)    p_model="claude-sonnet-5-5"; p_effort="low";    p_budget="1" ;;
  reviewer) p_model="claude-opus-5-5";   p_effort="medium"; p_budget="2" ;;
  planner)  p_model="claude-opus-5-5";   p_effort="medium"; p_budget="3" ;;
  cheap)    p_model="claude-haiku-5-5";  p_effort="low";    p_budget="0.25" ;;
  hard)       p_model="claude-opus-5-5";  p_effort="low";    p_budget="3" ;;
  very-hard)  p_model="claude-opus-5-5";  p_effort="medium"; p_budget="5" ;;
  impossible) p_model="claude-fable-5-1"; p_effort="medium"; p_budget="6" ;;
  *) echo "run.sh: unknown profile '$profile' (cheap, light, worker, reviewer, planner, hard, very-hard, impossible)" >&2; exit 1 ;;
esac
model="${model:-$p_model}"; effort="${effort:-$p_effort}"; budget="${budget:-$p_budget}"

if [ $# -gt 0 ]; then prompt="$*"; else prompt="$(cat)"; fi
[ -n "$prompt" ] || { echo "run.sh: empty prompt" >&2; exit 1; }

key="${CLAUDE_CREDIT_WORKER_KEY:-}"
if [ -z "$key" ] && [ -r "$CONF_DIR/key" ]; then
  key="$(tr -d '[:space:]' < "$CONF_DIR/key")"
fi
if [ -z "$key" ] && command -v security >/dev/null 2>&1; then
  key="$(security find-generic-password -s "$KEY_SERVICE" -w 2>/dev/null || true)"
fi
if [ -z "$key" ]; then
  echo "run.sh: no API key. Store it once, in your own terminal:" >&2
  echo "  macOS: security add-generic-password -U -a \"\$USER\" -s $KEY_SERVICE -w" >&2
  echo "  Linux: mkdir -p ~/.config/claude-credit-worker && (umask 077; read -rs k && printf %s \"\$k\" > ~/.config/claude-credit-worker/key)" >&2
  exit 2
fi

spent="$(month_spend)"
if awk -v s="$spent" -v b="$budget" -v c="$MONTHLY_CAP" 'BEGIN{exit !(s+b>c)}'; then
  echo "run.sh: monthly cap: \$$spent spent this cycle + \$$budget this run > \$$MONTHLY_CAP (raise MONTHLY_CAP in $CONF_DIR/config, or treat lane 2 as out)" >&2
  exit 77
fi

if [ "$write" -eq 1 ]; then
  mode="acceptEdits"; tools=(Read Glob Grep Edit Write Bash)
else
  mode="default";     tools=(Read Glob Grep)
fi
tools+=(${allow[@]+"${allow[@]}"})

flags=()
[ "$bare" -eq 1 ] && flags+=(--bare)

preamble="You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate, codex-worker or claude-credit-worker skills. Do not commit or push. Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran."

out="$(
  cd "$cwd" || exit 1
  unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
  export ANTHROPIC_API_KEY="$key"
  export DELEGATE_DEPTH=1
  printf '%s' "$prompt" | claude ${flags[@]+"${flags[@]}"} -p \
    --output-format json \
    --model "$model" \
    --effort "$effort" \
    --max-budget-usd "$budget" \
    --no-session-persistence \
    --append-system-prompt "$preamble" \
    --permission-mode "$mode" \
    --allowedTools "${tools[@]}" \
    --disallowedTools "Bash(git push:*)" "Bash(git commit:*)" 2>&1
)"
rc=$?

result="$(printf '%s' "$out" | jq -r '.result // empty' 2>/dev/null || true)"
is_err="$(printf '%s' "$out" | jq -r '.is_error // false' 2>/dev/null || echo false)"
cost="$(printf '%s' "$out" | jq -r '.total_cost_usd // 0' 2>/dev/null || echo 0)"
turns="$(printf '%s' "$out" | jq -r '.num_turns // 0' 2>/dev/null || echo 0)"
text="${result:-$out}"

if awk -v c="$cost" 'BEGIN{exit !(c>0)}'; then
  mkdir -p "$CONF_DIR"
  printf '{"ts":"%s","model":"%s","cost":%s}\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$model" "$cost" >> "$LEDGER"
fi

if [ "$rc" -ne 0 ] || [ "$is_err" = "true" ]; then
  if printf '%s' "$text" | grep -qiE 'credit balance (is )?too low'; then
    mkdir -p "$CONF_DIR"
    date -j -f %Y-%m-%d "$(next_reset)" +%s > "$OUT_MARK" 2>/dev/null || date -d "$(next_reset)" +%s > "$OUT_MARK" 2>/dev/null || true
    echo "$text" >&2; echo "run.sh: credit exhausted (back at the $(next_reset) reset)" >&2; exit 75
  fi
  if printf '%s' "$text" | grep -qiE 'rate.?limit|overloaded|\b429\b|\b529\b'; then
    echo "$text" >&2; echo "run.sh: rate limited" >&2; exit 76
  fi
  echo "$text" >&2; exit 1
fi

rm -f "$OUT_MARK"
printf '%s\n' "$text"
echo "cost=\$$cost turns=$turns month=\$$(month_spend)/\$$MONTHLY_CAP" >&2
exit 0
