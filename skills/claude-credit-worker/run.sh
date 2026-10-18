#!/usr/bin/env bash
# Run one task with `claude -p` on the Anthropic API credit, never on the Max login.
# --bare = only ANTHROPIC_API_KEY authenticates (OAuth and keychain login are never read).
#
# usage: run.sh [--write] [--model ID] [--effort LEVEL] [--budget USD] [--cwd DIR] [--] "prompt"   (or prompt on stdin)
# stdout: the worker's final answer.  stderr: one status line "cost=$X turns=N".
# exit:   0 ok | 75 credit exhausted | 76 rate limited or overloaded | 2 key missing | 1 other failure
set -uo pipefail

KEY_SERVICE="anthropic-worker-key"
model="claude-sonnet-5-5"
budget="2"
effort=""
cwd="$PWD"
write=0

while [ $# -gt 0 ]; do
  case "$1" in
    --write)  write=1; shift ;;
    --model)  model="$2"; shift 2 ;;
    --effort) effort="$2"; shift 2 ;;
    --budget) budget="$2"; shift 2 ;;
    --cwd)    cwd="$2"; shift 2 ;;
    --)       shift; break ;;
    *)        break ;;
  esac
done

if [ $# -gt 0 ]; then prompt="$*"; else prompt="$(cat)"; fi
[ -n "$prompt" ] || { echo "run.sh: empty prompt" >&2; exit 1; }

key="${CLAUDE_CREDIT_WORKER_KEY:-}"
if [ -z "$key" ]; then
  key="$(security find-generic-password -s "$KEY_SERVICE" -w 2>/dev/null || true)"
fi
if [ -z "$key" ]; then
  echo "run.sh: no API key. Store it once, in your own terminal:" >&2
  echo "  security add-generic-password -U -a \"\$USER\" -s $KEY_SERVICE -w" >&2
  exit 2
fi

if [ "$write" -eq 1 ]; then
  mode="acceptEdits"
  tools=(Read Glob Grep Edit Write Bash)
else
  mode="default"
  tools=(Read Glob Grep)
fi

out="$(
  cd "$cwd" || exit 1
  unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
  export ANTHROPIC_API_KEY="$key"
  extra=()
  [ -n "$effort" ] && extra=(--effort "$effort")
  printf '%s' "$prompt" | claude --bare -p \
    --output-format json \
    --model "$model" \
    ${extra[@]+"${extra[@]}"} \
    --max-budget-usd "$budget" \
    --no-session-persistence \
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

if [ "$rc" -ne 0 ] || [ "$is_err" = "true" ]; then
  if printf '%s' "$text" | grep -qiE 'credit balance (is )?too low'; then
    echo "$text" >&2; echo "run.sh: credit exhausted" >&2; exit 75
  fi
  if printf '%s' "$text" | grep -qiE 'rate.?limit|overloaded|\b429\b|\b529\b'; then
    echo "$text" >&2; echo "run.sh: rate limited" >&2; exit 76
  fi
  echo "$text" >&2; exit 1
fi

printf '%s\n' "$text"
echo "cost=\$$cost turns=$turns" >&2
exit 0
