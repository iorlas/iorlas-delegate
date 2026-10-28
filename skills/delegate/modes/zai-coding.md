# Z.ai GLM Coding Plan

Uses OpenCode headlessly with the `zai-coding-plan` provider and saved subscription credential.
User setup: `opencode auth login`, select **Z.AI Coding Plan**, enter the Coding Plan key privately;
then `route.py config set zai_coding_enabled true`.

Worker order: free DeepSeek → GLM-5.3 high → existing Codex/Grok/Claude fallbacks.
Light work: free DeepSeek → GLM-5.3-Flash low → existing fallbacks.
Review accepts `--authored-by zai` and prefers another family. Planner escalation stays unchanged.
No ordinary `zai` API-credit fallback. Limits are unknown until a vendor refusal; normal lane backoff applies.
The current global four-worker cap is below the supplied GLM-5.3 (30) and Flash (50) concurrency caps.
Do not infer remaining token allowance from concurrency. Lite Yearly is subscription billing, not prepaid API credit.
