# Founder-approved product decisions — 2026-10-15

Status: **Architecture direction approved; implementation and deployment NOT approved.**

1. Initial consumers: AJ Personal Assistant only. External API is deferred; no external API product roadmap unless the founder changes direction.
2. Routing: enforce task-specific minimum quality, then minimize expected total cost per independently validated successful task.
3. Exhausted budget: block further billable work and request upgrade or explicit authorization; no silent overspend.
4. Model selection: automatic and hidden from end users; preserve internal auditable decision records.
5. Expensive reasoning: escalating model tiers subject to quality, authorization, total workflow budget and bounded attempts.
6. Learning: public benchmark evidence plus independently validated task outcomes. Public scores are priors, not proof of production success.
7. Open-weight models: hosted external APIs initially; no GPU self-hosting by default.
8. Budget ledger outage: fail closed for all new billable inference.
9. Commercial scope: internal infrastructure only, although the code repository is public. No managed API commercialization.
10. Primary metric: lowest total cost per independently validated successful task, within hard security, privacy, quality, latency, reliability and budget constraints.

## Decision implications

- Build a private, authenticated service-to-service integration; no public inference endpoint.
- Privacy and provider permissions are independent of model score and cost.
- A hidden model choice does not mean opaque billing or unobservable internal operations.
- A user authorization must be bound to a specific scope, budget increase and expiry, with auditable consent.
- A cost ceiling is not a substitute for quota/rate/concurrency protection.
- Existing AJ Personal Assistant routing remains authoritative until shadow evaluation and explicit cutover approval.
- A public GitHub repository must not contain credentials, user prompts, customer data, private performance history or proprietary code.
