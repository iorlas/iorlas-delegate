# mode: codex (lane OPENAI_SUBSCRIPTION)

GPT-6.1 Sol and GPT-6 Luna on the ChatGPT Plus plan through the Codex CLI. Not Max, not the API credit.
`route.py run` executes every Codex step with `codex exec --json`: brief on stdin, stdin then closed, timeout kills the process group, a limit refusal marks the lane out, HEAD and branch compared around it. You never build the command.

- Continue earlier work: `--resume REF` (the `ref` of an earlier result); same lane and model, a lane that is out answers a `stop` (`rate_limited`, `held`, ...).
- Parallel writers: one directory each, made by you, as `--cwd`.
- Luna: big allowance, weak (13.6% Terminal-Bench 4.0). Reading and summarising only.
- Optional, by hand: the Codex plugin (`/codex:review`, `/codex:adversarial-review`, `codex-companion.mjs task --write` in a Bash `run_in_background`). Not `/codex:rescue --background`: jobs stayed queued forever (2026-10-14).
- `codex exec` waits forever on an open stdin pipe, and killing only `codex` leaves its vendor binary running (reproduced 2026-10-18). `run` handles both.
- Not yet seen: Codex's exact usage-limit wording. `run` matches `usage limit|rate limit|quota|429|try again later`; when the real text appears, add it here and in `LIMIT_RE`.
- Codex refuses a non-git directory; `run` adds `--skip-git-repo-check` there.
- Never trust Codex's summary: read the diff, rerun the acceptance check.
