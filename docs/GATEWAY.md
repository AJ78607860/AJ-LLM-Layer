# LLM Layer execution gateway (v0.2)

One place every AJ app sends its model calls: AJ Assistant, Calibre and anything built later.
Apps never hold provider keys or vendor SDKs; they call the Layer, and the Layer picks the
provider, falls back when one fails, and logs metadata only.

## What it does

- **Providers:** OpenAI (Chat Completions) and Anthropic (Messages API), raw HTTP via `httpx`.
- **Automatic fallback:** tries providers in order (default `openai,anthropic`).
  - Out of credits, rate limit, bad key, timeout, outage → next provider.
  - Timeout / 5xx → one quick retry on the same provider first.
  - Bad request or content refusal → returned immediately (another provider won't help).
- **Circuit breaker:** a provider that runs out of credits or fails auth is skipped for 10
  minutes (rate limits 20 s), so later calls go straight to the backup without paying the
  failed round-trip each time.
- **Privacy gate (ADR-025):** every request carries `data_class`, defaulting to `personal`.
  Personal data only goes to providers in `AJ_LLM_PERSONAL_DATA_PROVIDERS` (default
  `openai`). **Anthropic receives personal data only after the owner adds it there.**
- **Tiers:** apps ask for `fast`, `standard` or `smart`; the Layer maps that to each
  provider's model. Apps can also pin one provider + model.
- **Content-free logs:** app, purpose, tier, provider, model, outcome, latency, tokens.
  Never prompts, replies or keys.

## Two ways to use it

**Over HTTP (recommended; any language):**

```python
from aj_llm_layer.client import LLMClient
llm = LLMClient(base_url=os.environ["AJ_LLM_LAYER_URL"], api_key=os.environ["AJ_LLM_LAYER_KEY"])
reply = llm.chat("Summarise this email: ...", system="You are AJ.",
                 purpose="email_summary", tier="fast", data_class="personal")
reply.text, reply.provider, reply.model, reply.fallback_used, reply.usage
```

```
POST /v1/complete
Authorization: Bearer <app key>
{"messages":[{"role":"user","content":"..."}], "system":"...", "purpose":"email_summary",
 "tier":"standard", "data_class":"personal", "max_tokens":1024, "json_mode":false}
→ 200 {"text","provider","model","finish_reason","usage","cost_usd","latency_ms","fallback_used","attempts"}
→ 503 {"error":"quota", "attempts":[...]}   (also 400/422/429/502/504 by failure kind)
GET /v1/status   → per-provider key present (yes/no), models, approval, cool-down
```

**In-process (Python services that embed the package):**

```python
from aj_llm_layer.gateway import Gateway, CompletionRequest, Message
gateway = Gateway()
gateway.complete(CompletionRequest(messages=(Message(role="user", content="hi"),)), app="aj_assistant")
```

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `AJ_LLM_GATEWAY_ENABLED` | Turns on `/v1/*` (404 otherwise) | off |
| `AJ_LLM_GATEWAY_KEYS` | Per-app keys `aj_assistant:<key>,calibre:<key>` (≥32 chars each) | none → 503 |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Provider keys | none → provider skipped |
| `AJ_LLM_PROVIDER_ORDER` | Preference order | `openai,anthropic` |
| `AJ_LLM_OPENAI_MODEL_FAST/STANDARD/SMART` | OpenAI model per tier | **must be set** (no guess) |
| `AJ_LLM_ANTHROPIC_MODEL_FAST/STANDARD/SMART` | Anthropic model per tier | `claude-haiku-5-5` / `claude-sonnet-5-5` / `claude-opus-5-5` |
| `AJ_LLM_PERSONAL_DATA_PROVIDERS` | Providers approved for personal data | `openai` |
| `AJ_LLM_PRICES` | `{"model":[usd_in_per_M, usd_out_per_M]}` for cost reporting | unknown cost |

Generate an app key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Going live (each step needs owner approval)

1. Merge this PR after review.
2. Staging: set the variables above on the Render staging service, deploy, then run
   `python -m aj_llm_layer.gateway.smoke` (a few tokens per provider) and
   `--fallback`.
3. Decide on personal data: keep Anthropic for non-personal only, or add it to
   `AJ_LLM_PERSONAL_DATA_PROVIDERS` once a DPA is in place (amends ADR-025).
4. Point AJ Assistant's model calls at `LLMClient` behind a feature flag, staging first,
   keeping today's direct OpenAI path as the fallback until certified.
5. Production deploy and switch-over.

## Not in v0.2 (next)

Streaming, tool/function calling, images/audio, shared (Redis) circuit-breaker state across
replicas, per-app budgets via CostOS, and wiring the routing engine's recommendation into
tier choice. AJ Assistant's tool-using turns need tool calling before they can move over.
