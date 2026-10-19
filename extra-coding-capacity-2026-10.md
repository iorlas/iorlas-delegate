# Extra coding capacity on top of Claude Max 20x — research, 2026-10-14

Budget: up to $120/mo extra. Floor: Claude Sonnet 5.5 quality. Workload: Python, Unity C#, Godot, agentic coding.
Method: 5 Sonnet research agents, one pass each, not cross-checked. Prices from vendor pages where fetched, else third-party (marked).

## Already owned

- Max 20x includes $200/mo Anthropic API credits (official: platform.claude.com/docs/en/about-claude/api-credits-for-subscribers).
  Covers Claude API, Agent SDK, Managed Agents. Does NOT cover interactive Claude Code. No rollover.
  → Any headless / scripted agent work should run here first, at Opus 5.5 quality.
- Effort setting matters more than model swap: AA Coding Agent Index, Claude Code + Sonnet 5.5 scores 68.4 at max, 62.9 at xhigh, 55.0 at high. Default is medium.

## Quality yardstick (independent runs only)

| model | Terminal-Bench 4.0 (BenchLM snapshot 10-12) | Terminal-Bench 4.0 (Vals, 10-13) | AA Coding Agent Index (agent+model) | GameDevBench (Godot) |
|---|---|---|---|---|
| Opus 5.5 | 64.9 | 65.2 | 66.0 | not tested |
| Sonnet 5.5 | 61.8 | 64.1 | 68.4 (max) | not tested |
| GPT-6.1 Sol (Codex) | 58.2 | 55.1 | 62.9 | 70.0 (rank 1) |
| GPT-6 Astra (Codex) | 58.2 | — | 61.6 | 68.8 |
| Gemini 4 Argon | — | 57.6 | 63.8 | — |
| GLM-5.3 | 41.8 | 38.9 | 53.6 | — |
| Grok 4.7 | 37.6 | 28.8 | 56.3 | — |
| Kimi K3 | — | 17.2 | 51.9 | 58.0 |
| DeepSeek V4.1 Flash | — | 19.7 | — | — |

Not measured: Unity C# (no public benchmark exists). Vendor self-reports for GLM/Kimi/DeepSeek (Terminal-Bench 2.1 ~88, SWE-bench Verified ~94) use older benchmark versions and are not comparable. SWE-bench Verified is saturated and archived; SWE-bench Pro cross-vendor numbers are all self-reported.

## Options

| option | $/mo | model | quota | harness | verdict |
|---|---|---|---|---|---|
| GitHub Copilot Max | 100 | Sonnet 5.5, Opus 5.5 (real Claude) | 20,000 credits = $200 at Anthropic API rates; half is "flex", may vary monthly | VS Code, JetBrains, Copilot CLI | ✅ only option with guaranteed Sonnet 5.5 quality and a known number |
| GitHub Copilot Pro+ | 39 | same | 7,000 credits = $70 | same | ✅ cheap version of the above |
| Codex Plus | 20 | GPT-6.1 Sol, GPT-6 Astra | Sol 15–160 msgs / 5h; weekly cap unpublished | Codex CLI | ✅ trial; best measured on Godot |
| Codex Pro 100 | 100 | same | no 5h limit "currently"; weekly cap unpublished | Codex CLI | ⚠️ only if Plus limits bite; volume unknown |
| Google AI Ultra 5x | 100 | Gemini 4 Argon; Antigravity also lists Sonnet/Opus 5.5 (secondary source) | opaque compute pool; reports of fast drain | Antigravity CLI/IDE | ❌ can't measure what you buy |
| Z.ai GLM Coding Plan Pro | 80 (list) | GLM-5.3 | 290–580M tokens/week (vendor, 95% cache) | Claude Code via ANTHROPIC_BASE_URL | ❌ below the floor (~65% of Sonnet on TB4); good bulk volume |
| DeepSeek API | pay-go | V4 Pro / V4.1 Flash | $120 ≈ 0.4–0.8B input tokens (Pro) | Claude Code via endpoint | ❌ below the floor; independent scores conflict |
| Kimi Code, MiniMax, Qwen | 14–120 | K3, M3, Qwen3.8 | large | Claude Code via endpoint | ❌ weakest measured, or unmeasured |
| Cursor Pro+ | 60 | Claude at API rates | ~$70 pool (unconfirmed) | Cursor IDE/CLI | ❌ worse ratio than Copilot |
| Grok, Mistral, Augment, Zed, Warp, Kiro | — | — | — | — | ❌ weak model or ≤1:1 pool ratio |

## Recommended stack ($120)

1. Copilot Max $100 — real Sonnet 5.5 / Opus 5.5, ~$200 of API-rate usage. Opt out of training (default on since 2026-04-24).
2. Codex Plus $20 — GPT-6.1 Sol as second opinion and Godot work; measure its weekly cap for a week.
3. Alternative after a month: if Codex Plus limits bite and Sol output is good enough, swap to Codex Pro 100 + Codex Plus dropped ($100), keeping $20 headroom. Copilot Pro+ + Codex Pro 100 = $139, over budget.

## Revised for the orchestrator setup (Opus in Claude Code → MCP server → worker models via OpenCode / CLIs, Sonnet reviews)

What matters now: headless driving, terms on automated use, concurrency. Harness preference no longer matters.

| worker source | $/mo | headless path | terms on automation | verdict as worker |
|---|---|---|---|---|
| Anthropic API credits (already owned) | 0 | Agent SDK / API key | allowed (it is the API) | ✅ first: $200/mo of Sonnet/Opus 5.5 at list price, free |
| Codex Plus → Pro 100 | 20 → 100 | `codex exec`; OpenCode "ChatGPT Plus/Pro" login (official on OpenCode side; OpenAI "Sign in with ChatGPT" DevDay 2026-09-29, primary page not read) | no ban found | ✅ GPT-6.1 Sol: ~4 pts under Sonnet 5.5 on TB4, rank 1 on Godot bench; subscription volume likely above API-rate value (unmeasured) |
| Z.ai GLM Coding Plan | 18 / 80 | official OpenCode provider | "only officially supported tools" (secondary quote); OpenCode is listed | ⚠️ bulk worker under a written spec; below the floor solo |
| GitHub Copilot Max | 100 | `copilot -p` documented for scripts/CI | CLI automation documented; OpenCode login unofficial on GitHub side | ⚠️ exactly 2x API list price (official table: Sonnet 5.5 $2/$0.10 cached/$10 out); same tokens you already get free via API credits |
| DeepSeek API | pay-go | API key | normal API | ⚠️ cheapest volume; fell far behind on the fresh-task bench (V4 Pro 40.2 vs Sonnet 5 56.8) |
| Kimi, MiniMax | 14–120 | OpenCode | no ban found | ❌ weak or unmeasured |
| Google AI Ultra / Antigravity | 100 | `agy -p` | ❌ wrapping banned on consumer plans; suspensions reported | ❌ |
| Claude Max in OpenCode | — | — | ❌ prohibited | ❌ |

Practitioner evidence (one pass):
- SWE-rebench (fresh tasks May–Jul 2026, prior-gen models): GLM-5.2 62.9 > GPT-5.6 Sol 62.3 > Sonnet 5 56.8 > MiniMax M3 47.2 > DeepSeek V4 Pro 40.2. No 5.5-gen entries yet.
- Planner/executor (Akita On Rails, 2026-04, older models): Opus 4.7 plans → GLM 5.1 executes = 93 vs Opus solo 97, at ~1/8 the cost. Open models viable executors with a written plan, a few points lost.
- Usage share (OpenRouter, Kilo): dominated by free / Flash models — measures price, not quality.
- No public Unity C# or Godot practitioner data except GameDevBench.

Recommended month 1 ($100):
1. Workers on the free $200 API credits (Sonnet 5.5) via Agent SDK.
2. Codex Plus $20 — measure the weekly cap under `codex exec`.
3. GLM Coding Plan Pro $80 via OpenCode — bulk worker, Sonnet reviews.
After a month, move money to whichever runs out first (Codex Pro 100 if Sol carries the load).

MCP wrappers found (all tiny, vet before shell access): alejandro-technology/opencode-mcp, lakpriya1s/agent-rack, agent-runbooks/throng-mcp, blackaxgit/agent-mcp-hub, sebstaq/opencode-subagent-mcp.

## Claude Code with third-party endpoints

Still works via ANTHROPIC_BASE_URL (code.claude.com/docs/en/llm-gateway). Anthropic does not support non-Claude models through it. Reported gaps: images, some MCP, cache_control on DeepSeek (unverified README).

## Open / unverified

- Copilot CLI draws from the same credit pool — likely, not confirmed.
- Turkey card acceptance — unchecked for every vendor.
- Copilot "flex" credits can shrink month to month.
- Codex weekly caps — unpublished; measure.

## Sources

- https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers
- https://docs.github.com/en/copilot/get-started/plans
- https://github.com/orgs/community/discussions/197089
- https://learn.chatgpt.com/docs/pricing
- https://justinmckelvey.com/blog/codex-usage-limits
- https://www.vals.ai/benchmarks/terminal-bench-4
- https://benchlm.ai/benchmarks/terminal-bench-4
- https://benchlm.ai/benchmarks/aacodingagents
- https://benchlm.ai/benchmarks/gamedevbench
- https://www.anthropic.com/claude-sonnet-5-5
- https://antigravity.google/blog/changes-to-antigravity-plans/
- https://docs.z.ai/devpack/overview
- https://api-docs.deepseek.com/quick_start/pricing
- https://code.claude.com/docs/en/llm-gateway
- https://cursor.com/docs/models-and-pricing
- https://synthetic.new/pricing
- https://ollama.com/pricing

## Round 3 — DeepSeek, other Chinese labs, Meta, Google (2026-10-14, one pass each)

Typical worker task = 200K input (90% cached) + 20K output. Sonnet 5.5 = $0.258.
TB4 = Terminal-Bench 4.0 on Vals (one shared harness, 2026-10-13). AA = Artificial Analysis Coding Agent Index (each vendor's own harness). Not comparable with each other.

| model | TB4 | AA | cost/task | access for workers | data | verdict |
|---|---|---|---|---|---|---|
| Sonnet 5.5 (reference) | 64.1 | 68.4 | $0.258 | Agent SDK on API credits | no train | ✅ |
| Meta Muse Wonder Full | ~57–59 | 68 | no per-token price; Muse Code Power plan (~$50, third-party price) | `muse exec`; subscription in scripts undocumented | Contributor mode trains by default, turn off | ⚠️ released 2026-10-12; test one month |
| Meta Muse Wonder (API) | — | 63 | ~$0.40 ($4/$0.20/$20) | OpenCode with META_API_KEY, documented | Standard tier no-train | ❌ dearer than Sonnet |
| Gemini 4 Argon | 57.6 | 63.8 | $0.258 now, $0.516 from 2026-12-01 | Gemini API key | paid no-train | ❌ Sonnet price, lower score |
| Gemini 3.8 Flash | — | — | $0.104 (doubles 2027-01-01) | Gemini API key | paid no-train | ⚠️ tool-call errors reported |
| GLM-5.3 / Flash | 38.9 / 37.4 | 53.6 | plan | OpenCode | unconfirmed | ⚠️ bulk worker |
| Step 5 Preview (StepFun) | 31.8 | 51.2 | Step Plan $6.99–$99 | OpenCode + Claude Code documented | not found | ⚠️ best new Chinese model by score |
| Xiaomi MiMo V2.6 Pro | 31.3 | — | ~$0.03; plan $6–$100 | OpenCode | not found | ⚠️ best per $ |
| DeepSeek V4.1 Flash | 19.7 (vendor 31.2) | — | $0.016 off-peak | OpenCode / Anthropic endpoint; integration bugs (effort dropped, reasoning replay 400s) | ToS lets DeepSeek train on inputs; China law | ⚠️ cheap, bounded tasks only, not private code |
| Qwen3.8 Max | 34.3 | 43.3 | ~$0.10 | Coding Plan bans automation; Token Plan Personal trains | — | ❌ |
| Kimi K3 / MiniMax M3 / Hy4 | 17.2 / 1.0 / 8.1 | — | — | — | — | ❌ |
| KAT-Coder-Pro V2.5 (Kuaishou) | not on boards | — | ~$0.07 | OpenRouter | — | untested |

## Round 4 — credits usage, local models, discounts, CN/JP/RU forums (2026-10-14, one pass each)

### API credits — corrected
Help Center 17154008 (verified): credits cover `claude -p` and Agent SDK when run with an API key from the linked Console org. Not covered: interactive Claude Code, desktop app, IDE extension, GitHub Action (even with `-p`), or anything signed in with the plan.
- Key isolation: key only in the worker process env (MCP server `env` block or keychain at spawn); separate `CLAUDE_CONFIG_DIR`; check `claude auth status`; dedicated Console workspace + spend limit; no auto-reload.
- Known billing-surprise issues: anthropics/claude-code #37686, #86723, #43333.
- Measured (one practitioner, vincentvandeth.nl): Sonnet 5.5 dispatch median $0.33 → $200 ≈ 320 Sonnet dispatches or ≈ 50 Opus.
- Stretch: Batch API −50%; cache reads 0.05x on Sonnet/Opus 5.5; Haiku 5.5 $0.10/$0.50 under 100K prompt (5x above); Managed Agents $0.08/session-hour, no batch discount.

### Local models (M3 Max, 36 GB)
- Best fit: Qwen3.8-27B 4-bit (~17 GB), ctx 32–64K, ~10–20 tok/s gen (extrapolated), prompt processing ~160–200 tok/s → 100K cold prompt ≈ 10 min.
- GLM-5.3-Flash does not fit (smallest quant 93 GB).
- Verdict: ❌ as agentic worker; ok for single-file tests, docstrings, summaries under 32K. Homelab: a used RTX 3090 24 GB runs Qwen3.8-27B with ~13x faster prompt processing.

### Discounts
- No lira/regional saving (Plus ₺999.99 ≈ $20.4).
- Codex Pro 100: one measurement (inventivehq, low confidence) ≈ $330–380 API-equivalent per week (~14x price); no 5h window "for the upcoming months". Plus has a 5h window again since Aug 25.
- Z.ai: annual ~30% off (prepay), 10% invite on first order; usage policy: officially supported tools only, scripted/SDK access can trigger throttling, >3 violations → ban. Headless `opencode run` not addressed — ask support in writing.
- Codex for Open Source (6 months Pro + API credits) and Copilot free for popular OSS maintainers — apply if eligible.
- Student programs: not eligible.

### Forum patterns worth copying
- Habr 2026-08-09 `/cdx`: Claude writes PLAN.md with testable acceptance criteria → Codex read-only adversarial plan review (BLOCKER/MAJOR/MINOR, APPROVE/REVISE) → Codex builds in one persistent thread, banned from commit/push/secrets/weakening tests → Claude reads the diff and runs tests itself → fixes go back to the same thread; two failed fixes → back to plan.
- Official openai/codex-plugin-cc (34k stars): `/codex:review`, `/codex:adversarial-review`, `/codex:rescue` inside Claude Code.
- oh-my-openagent (70k stars): route by task category with fallback chains across providers; treat 429 as normal.
- cc-switch (144k stars), claude-code-router (38k): provider switching / routing proxies.
- Harness matters: same DeepSeek V4 Pro 69 in OpenCode vs 84–89 in Claude Code harness (akitaonrails, older models); but cache-less proxy made it ~3x Opus cost per task. Measure cost per accepted diff.
- Cross-family reviewer (Codex reviewing Claude work or vice versa) preferred over same-family.
- Relays / shared accounts (中转, 拼车, Russian resellers): ToS violation; documented router attacks (arXiv 2604.08407: tool-call injection, credential theft) and fake models (arXiv 2603.01919: 45.83% identity failures). Avoid.

### Final stack ($118)
1. Codex Pro 100 — $100. GPT-6.1 Sol workers via `codex exec` / codex-plugin-cc; cross-family reviewer.
2. GLM Coding Plan Lite monthly — $18. Bulk worker via OpenCode, pending written OK from Z.ai on headless use.
3. Sonnet 5.5 workers via `claude -p` + API key — $0 (the $200 credit).
Dropped: Copilot Max ($200 at list price, below Codex's measured value), DeepSeek for private code (trains on inputs), local models, Google, Meta (re-check Muse Wonder Full after a few weeks of reports).

## Round 5 — apples-to-apples cost per solved task (Vals Terminal-Bench 4.0, 2026-10-13)

One harness (mini-SWE-agent), avg@3, API list prices. $/solved = cost per test ÷ pass rate. Hard multi-step terminal tasks, so absolute $ is high; use the ratios.
Not measured: Unity C#, Godot; subscription quotas (marginal cost 0 until the cap); a low pass-rate model may never solve the hardest tasks however many retries.

| model | pass | $/test | $/solved | monthly route |
|---|---|---|---|---|
| GPT-6.1 Sol | 55.1% | 1.72 | 3.12 | Codex Plus $20; Copilot (list price); OpenAI API |
| GPT-6 Astra | 59.6% | 9.58 | 16.07 | Codex (low quota on Plus) |
| Claude Opus 5.5 | 65.2% | 13.20 | 20.26 | Max (owned); $200 credits |
| Claude Sonnet 5.5 | 64.1% | 16.51 | 25.74 | Max (owned); $200 credits |
| Claude Haiku 5.5 | 36.4% | 3.12 | 8.58 | $200 credits |
| Meta Wonder Full | 58.1% | 10.53 | 18.13 | Muse Code plan ($50, unofficial price) |
| Gemini 4 Argon | 57.6% | 17.64 | 30.64 | Gemini API |
| GLM 5.3 | 38.9% | 9.37 | 24.09 | Z.ai Lite $18 / Pro $80 |
| GLM 5.3 Flash | 37.4% | 1.22 | 3.26 | Z.ai plan / API |
| MiMo V2.6 Pro | 31.3% | 0.50 | 1.60 | Xiaomi plan $6–$100 |
| MiMo V2.6 Flash | 24.2% | 0.22 | 0.91 | same |
| DeepSeek V4.1 Flash | 19.7% | 0.50 | 2.54 | API only (trains on inputs) |
| Step 5 Preview | 31.8% | 3.11 | 9.77 | Step Plan $6.99+ |
| Qwen 3.8 Max | 34.3% | 10.63 | 30.96 | API |
| Tencent Hy4 Preview | 8.1% | 2.10 | 26.00 | API |
| Kimi K3 | 17.2% | 12.48 | 72.70 | Kimi Code ~$19+ |
| MiniMax M3 | 1.0% | 7.23 | ~716 | M Plan $22+ |

Vals note: Opus 5.5 drops to 58.1% if 22 fallback-served attempts count as failures.

### Budget stack ≤ $60
1. Codex Plus $20 — GPT-6.1 Sol primary worker (best $/solved by 6x at near-Claude pass rate). 5h window.
2. GLM Coding Plan Lite $18 monthly — bulk lane (GLM 5.3 Flash), pending Z.ai written OK on headless use.
3. ~$20 OpenAI API pay-go — Sol overflow when the Plus window is spent ($1.72/test list).
4. $0 — Sonnet/Haiku via `claude -p` + API key on the $200 credit (reviewer, Haiku routing).
Total ≈ $58. Skip Codex Business (2-seat minimum, seats are per user).

### Plumbing answers
- OpenCode has no official MCP server mode found; `opencode serve` (HTTP, port 4096) + @opencode-ai/sdk; community wrappers: opencode-mcp, opencode-executor-mcp, mcp-subagents-opencode, opencode-agent-mcp (glama.ai listings; vet before use).
- Codex ships an official MCP server: `codex mcp-server`; and openai/codex-plugin-cc for Claude Code.
- No dominant `claude -p` harness among power users. Observed: small custom MCP/skill that spawns `claude -p --output-format json` with key-only env + separate CLAUDE_CONFIG_DIR (vincentvandeth.nl); wyrd-company/async-claude-agentsdk-mcp (async session_id pattern, 1 star); steipete/claude-code-mcp (1.3k, archived).

## Round 6 — wiring and cheaper Codex (2026-10-14, one pass each)

### Correction
The "every Codex plan ≈ 14x its price" figure is unverified: the cited inventivehq page contains no such claim. Plus allowance in API-$ is unknown; weekly cap unpublished. Only OpenAI's 15–160 Sol messages / 5h is official.

### Wiring cheap models into Claude Desktop
Recommended: one local MCP "workers" server (~100 lines) → spawns `claude -p --output-format json` per task with a provider env profile + its own CLAUDE_CONFIG_DIR, in a git worktree for write tasks. Tools: delegate, delegate_async → task_id, task_result / list / cancel (shape of aashahin/claude-opencode-delegate). GPT-6.1 Sol via openai/codex-plugin-cc side by side.
Anthropic: routing Claude Code to non-Claude models is unsupported but works. Per-subagent provider routing not shipped (issue anthropics/claude-code#38698).

Provider profiles (vendor docs, 2026-10-14; ids to confirm in each console):
- DeepSeek: ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic, model deepseek-flash / deepseek-v4-pro. Ignores cache_control, MCP tool blocks, parallel-tool flag.
- Z.ai: https://api.z.ai/api/anthropic, glm-5.3[1m], haiku slot glm-5.3-flash[1m], API_TIMEOUT_MS=3000000. Needs Coding Plan key.
- Xiaomi MiMo: https://api.xiaomimimo.com/anthropic (pay-go, sk- key) or https://token-plan-cn.xiaomimimo.com/anthropic (Token Plan), model mimo-v2.6-pro. V2.5 ids end 2026-10-21.
- StepFun Step Plan: https://api.stepfun.ai/step_plan (no /v1), step-3.7-flash; "Step 5 Preview" not found in Step Plan docs.
- Kimi: https://api.kimi.com/coding/, k3-256k.
Always set the token var too; BASE_URL alone keeps the saved claude.ai login active.

Alternatives: OpenCode (`opencode run` / `serve` / `acp`; wrapper aashahin/claude-opencode-delegate, 3 stars; DeepSeek multi-turn reasoning_content bug); Pi (badlogic/pi-mono, 115k stars, RPC mode, no existing Claude wrapper); Codex CLI can drive DeepSeek via wire_api="responses" (separate CODEX_HOME); A2A — no practical wrapper found; claude-code-router / cc-switch / pleaseai/shunt are model switchers, not delegation.

### Cheaper GPT-6.1 Sol
- ChatGPT Go: no Codex CLI / Sol. ❌
- Plus $20: allowance unknown; measure with /status.
- Codex credits: $0.04 each = list price, only after the allowance.
- API key: list $2/$0.10/$10; Flex/Batch ~50% but Codex CLI may strip Flex (openai/codex #26604, #31562, #37813). OpenAI docs recommend API keys for programmatic Codex; ChatGPT-login automation is a grey zone.
- Copilot Pro+ $39 → $70 at list (Pro $10 excludes Sol).
- Students (US/Canada) and Codex for OSS only.

### Revised stack ≤ $60
1. ChatGPT Plus $20 — Sol via codex-plugin-cc, interactive bursts; measure allowance 2 weeks.
2. OpenAI API key, prepaid $20 — Sol for automated workers (documented path), capped.
3. MiMo or DeepSeek pay-go, prepaid $10 — cheap bulk lane via `claude -p` profile (MiMo $1.60/solved; DeepSeek trains on inputs — non-private code only).
4. $0 — Sonnet / Haiku on the $200 Anthropic credit.
Total $50, all prepaid except Plus. GLM plan dropped: GLM 5.3 Flash ≈ Sol on $/solved at lower pass rate, and its plan terms are unclear on scripted use.
