# LLM Layer execution gateway (v0.4)

One place every AJ app sends its model calls: AJ Assistant, Calibre and anything built later.
Apps never hold provider keys or vendor SDKs. They call the Layer, and the Layer:

1. **picks the right model for the job** from every provider it has a key for. It goes by evidence of correct answers and by price. No provider is preferred by name.
2. **avoids providers with no credit** and falls back when a call fails.
3. **learns which model is best** for each kind of task from graded answers.

Logs and stored evidence contain metadata only. Prompts, replies and keys are never kept.

## Reading the question (no model call)

Apps can leave `task_family` and `tier` as `"auto"`, which is the default. The Layer then reads the question itself (`understand.py`), locally and in microseconds. It costs no tokens and sends nothing anywhere, in line with the Layer's zero-router-LLM rule.

- **Task family** comes from code, maths, translation, writing, extraction, JSON, research, planning, travel, finance and document cues. Ambiguous questions become `conversation`.
- **Tier:**
  - `smart` for high-stakes questions (health and emergencies, money and debt, legal and housing rights), irreversible actions (deleting or wiping data, cancelling, moving or committing money, trading), proofs, deep or multi-step requests, large code or very long inputs. The high-stakes word list is deliberately broad: a false alarm costs pennies, a miss can cost far more;
  - `fast` only for small talk ("hi", "thanks") and short translation, extraction and writing;
  - `standard` otherwise, including any general question that matches no category.
- **Accuracy (author-measured, not independent evidence):** on the 35 questions in `tests/fixtures/understand_cases.v1.json` it gets 34/35 families and 35/35 tiers. The questions and labels were written by the same author as the rules, and 5 tier labels were revised on 25 Nov after review (four general questions from fast to standard, one landlord email to smart). Treat this as a regression guard. A real accuracy figure needs an independently labelled set.
- **What the reply shows:** the family and tier used, and `understanding` (whether each came from the app or the Layer, the confidence, and the names of the rules that fired; never any text).
- **App labels win.** AJ, for example, can keep sending what its own analyser decided.
- **Escalation:** if a model's answer fails an objective check (it's empty, or JSON was asked for and the answer isn't valid JSON), the Layer grades it wrong and tries a stronger tier. If nothing passes, it returns the best answer it got.

The vocabulary follows AJ's existing analysers (`core/router/analysis.py` and `universal_ai_gateway/classifier.py`), generalised so every app gets the same behaviour.

## How a model is chosen (the "supermarket")

The **catalogue** (`aj_llm_layer/gateway/catalog.json`) lists every model the Layer can use. Each entry has its provider, a tier (`fast`, `standard` or `smart`), its price per million tokens, its context and output limits, and where and when its price was checked.

For each request, the app gives the kind of task (`task_family`, e.g. `conversation`, `coding`, `extraction`, `translation`, `writing`) and the tier it needs. The selector (`selector.py`) then orders the candidates in four steps:

1. **Hard gates.** It removes models that can't take the request:
   - the provider has no key;
   - the provider isn't approved for this data (personal data goes to approved providers only);
   - the provider is out of credit or over its spend limit;
   - the request is too long for the model.
2. **Proven models first.** Models with enough graded answers for this task family (`AJ_LLM_MIN_SAMPLES`, default 30) go through the existing routing engine (`aj_llm_layer/routing/engine.py`). The engine drops any model whose 95% lower bound on accuracy is below the bar (`AJ_LLM_MIN_QUALITY`, default 0.8), or that is unreliable or too slow. It ranks the rest by strategy:
   - `cost_efficient` (default): cheapest per *correct* answer;
   - `quality_first`: most accurate first;
   - `balanced` and `latency_sensitive` are also available.
3. **Untested models next, cheapest first.** These are models without enough evidence yet. About 5% of traffic (`AJ_LLM_EXPLORE_RATE`) tries an untested model first, so evidence keeps building.
4. **Proven-poor models last.** These have enough evidence and fall below the bar.

After that come **stronger tiers as backup**, never weaker ones.

The gateway tries the list in order. If a model fails in a way another model could fix (out of credit, rate limit, bad key, outage, unknown model), it moves to the next one. Bad requests and content refusals come back straight away.

High-stakes task families (`financial_analysis`) never get promoted on evidence. The engine keeps them for restricted, owner-approved models (roadmap decision D3). Until that exists they use the untested order.

## How it learns which model is best

- **Every call is measured automatically:** success or failure, timeouts, latency and real cost, recorded for each task family and model.
- **Correctness comes from grading.** Each reply includes a `call_id`. Send `POST /v1/feedback {"call_id": "...", "correct": true|false}` from the app's own checks, from a user's thumbs up or down, or from an evaluation run. A call can be graded once, and only by the app that made it.
- **Objective checks grade automatically.** If an answer was asked for as JSON and isn't valid JSON, it is graded wrong.
- **`GET /v1/models?task_family=coding`** is the leaderboard. For each model it shows price, calls, graded answers, accuracy and its lower bound, failure rate, p95 latency and cost per correct answer.

Grades should come from checks that are independent of the model being graded (roadmap rule 4). The engine won't promote a model until there are enough of them.

## Avoiding providers with no credit

OpenAI and Anthropic don't offer an API to read a prepaid balance, so the Layer uses these guards:

1. **Reactive bench (always on).** The first "out of credits" response benches that provider and **all of its models**:
   - The bench lasts 10 min, then 30 min, 2 h and 6 h on repeat failures.
   - While benched, calls skip it without wasting a round trip.
   - After each bench expires, one real call checks whether credit is back. A success clears the bench.
   - A bad key benches the same way. Rate limits bench for 20 s only.
   - Outages and timeouts don't bench. The gateway retries, then falls back.
2. **Proactive spend limit (optional).** Set `AJ_LLM_SPEND_LIMITS` a little under your prepaid balance for each provider. Spend is counted from token usage at catalogue prices. Once a provider reaches its limit for the month, it is skipped before it runs dry.
3. **Shared state.** With `AJ_LLM_STATE_REDIS_URL` set, every worker and replica sees the same benches, spend and evidence. Without it, each process tracks its own. If Redis goes down, the gateway falls back to memory and never blocks a reply.
4. **Topped up?** `POST /v1/admin/providers/<name>/reset` with the admin key (`AJ_LLM_ADMIN_KEY`) puts the provider back in use immediately.

## Using it

**Over HTTP (recommended, works from any language):**

```python
from aj_llm_layer.client import LLMClient
llm = LLMClient(base_url=os.environ["AJ_LLM_LAYER_URL"], api_key=os.environ["AJ_LLM_LAYER_KEY"])
reply = llm.chat("Summarise this email: ...", system="You are AJ.", purpose="email_summary",
                 task_family="writing", tier="fast", data_class="personal")
reply.text, reply.model, reply.selection, reply.fallback_used, reply.cost_usd
llm.feedback(reply.call_id, correct=True)   # when the app knows the answer was right or wrong
```

```
POST /v1/complete      {"messages":[...], "system":"...", "task_family":"writing", "tier":"fast",
                        "data_class":"personal", "max_tokens":1024, "json_mode":false,
                        "strategy": optional, "providers": optional narrowing, "model": optional pin}
  → 200 {"text","provider","model","selection","call_id","usage","cost_usd","latency_ms",
         "fallback_used","attempts"}
  → 503 {"error":"quota","attempts":[...]}   (also 400/422/429/502/504 by failure kind)
POST /v1/feedback      {"call_id","correct"}
GET  /v1/models        leaderboard
GET  /v1/status        per provider: key present, approval, available, bench, spend
```

**In-process (Python services):** `from aj_llm_layer.gateway import Gateway`, then `Gateway().complete(...)`, `.feedback(...)` and `.leaderboard()`.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `AJ_LLM_GATEWAY_ENABLED` | Turns on `/v1/*` (404 otherwise) | off |
| `AJ_LLM_GATEWAY_KEYS` | Per-app keys `aj_assistant:<key>,calibre:<key>` (32+ chars each) | none → 503 |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | Provider keys | none → provider skipped |
| `AJ_LLM_SELECTION` | `routed` (evidence and price) or `ordered` (fixed provider order) | `routed` |
| `AJ_LLM_STRATEGY` | `cost_efficient`, `quality_first`, `balanced`, `latency_sensitive` | `cost_efficient` |
| `AJ_LLM_MIN_QUALITY` / `AJ_LLM_MIN_SAMPLES` | Accuracy bar (lower bound) and graded answers needed | 0.8 / 30 |
| `AJ_LLM_EXPLORE_RATE` | Share of calls that try an untested model first | 0.05 |
| `AJ_LLM_CATALOG_PATH` / `AJ_LLM_DISABLED_MODELS` | Own catalogue file / switch models off | built-in / none |
| `AJ_LLM_PERSONAL_DATA_PROVIDERS` | Providers approved for personal data (ADR-025) | `openai` |
| `AJ_LLM_SPEND_LIMITS` | `{"openai":20,"anthropic":50}` USD per month | no limit |
| `AJ_LLM_STATE_REDIS_URL` | Share credit, health and evidence across replicas | in-memory |
| `AJ_LLM_ADMIN_KEY` | Owner key for the reset endpoint (32+ chars) | reset disabled |
| `AJ_LLM_PROVIDER_ORDER`, `AJ_LLM_<PROVIDER>_MODEL_<TIER>` | `ordered` mode only | `openai,anthropic` |

Generate a key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

**The catalogue needs care.**
- Anthropic prices were checked against Anthropic's pricing page on 2026-11-25.
- OpenAI model IDs (`gpt-6.1-terra`, `gpt-6.2-sol`, `gpt-6.2-astra`) and the standard-tier price for `gpt-6.1-terra` are marked `price_verified: false`. Confirm them (roadmap D4) before relying on OpenAI cost ranking.
- An unknown model ID doesn't break anything. The gateway falls through to the next model.

## Going live (each step needs owner approval)

1. Merge this PR.
2. Staging:
   - Set the variables on the Render staging service, including a Redis URL so evidence persists.
   - Move the service off the free plan.
   - Deploy.
   - Run `python -m aj_llm_layer.gateway.smoke`, which spends a few tokens per provider.
3. Decide on personal data: keep Anthropic for non-personal data only, or add it to `AJ_LLM_PERSONAL_DATA_PROVIDERS` once a DPA is in place (this amends ADR-025).
4. Point AJ Assistant's calls at `LLMClient` behind a feature flag, staging first. AJ's own model gateway has a hard-coded OpenAI-only rule for sensitive data, which needs the same decision.
5. Production deploy and switch-over.

## Not yet built

- Streaming.
- Tool/function calling. AJ's main conversation needs it before it can move over.
- Images and audio.
- Per-app budgets through CostOS.
- An offline evaluation runner that grades every catalogue model on fixed test questions, so the leaderboard fills without waiting for live traffic.
