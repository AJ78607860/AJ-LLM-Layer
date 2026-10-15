# AJ LLM Layer — standalone architecture

AJ Personal Assistant remains on its existing router until a separately reviewed integration PR is approved.

## Boundaries
- Zero additional LLM calls for task classification and routing.
- This repository is public: never commit credentials, private customer data, proprietary routing history, internal URLs, or secrets.
- No provider is activated by default. Caller-provided data classification is untrusted.
- Provider permission must be established by authenticated tenant policy, independently of routing scores.
- Model ranking is offline only until evidence provenance, auth, p95 latency, circuit breaking, retry accounting, and sovereign restrictions are enforced.
- Public benchmarks are priors, not permissions or validated real-world success.
- Persistent production intelligence uses managed PostgreSQL with migrations, retention and least-privilege roles; no ephemeral SQLite as production memory.
- Integrations: OpenRouter, LiteLLM and Portkey are prospective execution adapters; they are not currently configured or called.

## Deployment gate
Do not deploy to production or route user prompts until security review, synthetic end-to-end tests, key management, tenant isolation, rate limits, request signing, and fallback verification pass.
