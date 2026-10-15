# Target architecture v1 — AJ Personal Assistant internal inference plane

## Request path

AJ Personal Assistant (authenticated service identity and server-verified user/tenant)
-> admission controls (privacy, provider allowlist, rate and concurrency limits)
-> atomic budget reservation (request/workflow + hourly/daily/weekly/monthly/annual caps)
-> zero-token local task classification
-> candidate quality/security/latency filtering
-> cost-minimizing deterministic router
-> authorized provider adapter (direct first; LiteLLM/OpenRouter/Portkey optional)
-> independent task-specific validation
-> bounded escalation/fallback if needed and budgeted
-> actual cost settlement and durable audit event
-> outcome store for offline model intelligence updates.

**Ordering constraint:** budget admission occurs before any billable external call. Classification may precede reservation only if it is demonstrably local and cost-free; no provider egress before authorization.

## Logical components

1. Internal FastAPI control/data plane, private by default; mTLS or signed short-lived service credentials, replay protection and least privilege.
2. Policy engine: authoritative user identity, tenant, data class, region, provider permissions and per-task quality floor.
3. Atomic budget ledger: PostgreSQL transactions with scoped period reservations, idempotent holds, settlement, refunds and reconciliation; Redis is not the source of truth.
4. Registry: immutable provider/model versions, dated and sourced pricing, capabilities, data terms, availability.
5. Zero-token router: local taxonomy, quality confidence, estimated *full workflow* cost, p95 latency and reliability.
6. Adapter layer: direct provider API first; optional reviewed LiteLLM/OpenRouter/Portkey paths; never chain proxies by default.
7. Evaluation: deterministic synthetic tests, verified task outcomes, provenance and uncertainty; no provider self-rating as sole success evidence.
8. Learning: offline, confidence-aware per-task ranking and constrained bandit experiments only after shadow testing.
9. Telemetry: request ID, policy version, decision reason, billed usage, retries, fallback, budget, p50/p95/p99 and provider errors; redacted logs.
10. Background ingestion: verified public benchmark licensing, source/version timestamps, freshness checks, no prompt leakage.

## Budget correctness requirements

- Hard per-request and workflow limits; user/tenant/subscription scopes; hour/day/week/month/year (and optional lifetime) caps.
- Document whether calendar or rolling windows apply; calendar windows use UTC ledger boundaries unless product specifies otherwise.
- Reserve maximum authorized cost *before* execution using an atomic transaction across all applicable scopes.
- Estimate output/reasoning/tool/retry/fallback costs; bound actual spend by provider token limits and controlled attempts.
- Settle actual usage once; release unused reservations; reconcile delayed or missing provider usage conservatively.
- Charge all failed attempts to cost-per-success statistics; do not silently refund provider charges.
- For lost responses, retries and duplicate idempotency keys, preserve the hold until final state is known; avoid double spending.
- No ledger availability => no new billable requests.
- Exhaustion => request explicit, scoped and expiring approval/upgrade; no implicit authorization from conversation text.
- Streaming and provider billing may exceed estimates: enforce safe worst-case reservations or deny unsupported unbounded models.

## Model quality and economics

Eligible(m,t,u) requires privacy permission, supported capability, confidence-qualified quality threshold, provider health, p95 latency SLO, and sufficient budget.
Choose minimum estimated expected *total* validated-success cost among eligible candidates.
If no eligible candidate exists, decline or ask for authorization; do not silently lower the quality floor.
Escalation is bounded, validated, idempotent and included in a single workflow-level budget reservation.

## Deployment gates

No production traffic or credentials until:
- independent security review and approved data-egress matrix;
- multi-tenant isolation and authenticated internal API;
- atomic concurrent reservation tests, idempotency and lost-response recovery;
- benchmark provenance and quality calibration;
- provider contract tests, failure injection, rate limiting, timeouts and circuit breakers;
- cost settlement/reconciliation tests and shadow-mode evidence;
- verified Render staging config, rollback and explicit founder approval.
