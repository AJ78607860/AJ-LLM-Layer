# Economy-first routing for simple queries — founder policy

Status: Approved product direction; implementation and provider egress require separate review.

## Policy

For low-complexity, low-risk tasks, prefer the cheapest independently quality-qualified model, including competitive Chinese-provider models such as DeepSeek, Qwen, Kimi and GLM. Country of origin is neither a quality score nor permission to transmit user data. A provider must pass data residency, retention, consent, contractual and security policies before consideration.

Use deterministic local handling with **zero LLM calls** when possible (formatting, arithmetic, cached facts with valid freshness, structured retrieval).

## Candidate tiers

- **Tier 0, no LLM:** deterministic operations, trusted cache and safe local tools.
- **Tier 1, economy:** greetings, benign rewriting, simple translation, classification and short summaries; evaluate DeepSeek/Qwen/Kimi/GLM and other economical providers by measured total cost and task-specific quality.
- **Tier 2, general:** factual queries and moderately complex writing/reasoning; choose lowest-cost qualified candidate.
- **Tier 3, advanced:** complex coding, mathematics, research and agent workflows; require task-specific validation and bounded escalation.
- **Restricted:** sensitive personal, regulated, financial or enterprise content; only providers explicitly permitted by authenticated policy, regardless of price.

## Mandatory gates before any billable inference

1. Server-verified tenant/user identity and data classification.
2. Provider-specific privacy/region allowlist; no caller-provided downgrade.
3. Minimum task-specific independent quality lower bound and freshness requirements.
4. Capability, reliability, p95 latency and tool-use compatibility.
5. Atomic request and workflow budget reservation with daily, weekly, monthly and annual caps.
6. Bounded output, reasoning, retries and fallback cost.

Rank remaining candidates by estimated **total cost per validated successful task**, not input-token list price. Cost includes output/reasoning tokens, tool calls, retries, failed attempts and execution-channel fees. No candidate meeting all constraints => refuse or request scoped authorization, never silently relax quality/privacy/budget.

## Evaluation requirements

- Run identical original synthetic tasks across approved models.
- Record immutable model version, prompt class, measured billed cost, p50/p95 latency, objective success, independent validation provenance and confidence.
- Cold-start public benchmarks are priors; they do not grant data-egress permission or prove performance for AJ's workloads.
- Monitor quality drift, provider price changes, service degradation and failure rates.
- Do not activate a new provider, enable production learning exploration or change AJ Personal Assistant routing without review and explicit approval.

## Illustrative decisions (not measured rankings)

- 'Make this sentence friendlier' -> Tier 1 if non-sensitive and provider allowed.
- 'What is 17 + 25?' -> Tier 0 deterministic calculator.
- 'Summarize my private bank statement' -> Restricted; never send to an economy provider unless explicitly approved for that data class.
- 'Debug a complex production outage' -> Tier 3 with bounded budget and escalation.

No current provider prices, quality scores or production performance are asserted in this document.
