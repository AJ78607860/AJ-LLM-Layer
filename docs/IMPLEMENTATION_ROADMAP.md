# Prioritized roadmap — 2026-10-15

## Current verified scope
- Draft PR #1: FastAPI service skeleton, offline quality/cost/p95 ranking, fail-closed provider stub, synthetic tests, staging blueprint. Tests not verified as passing.
- Draft PR #2 (stacked on #1): provider-neutral contract, objective synthetic evaluation, per-task validated-outcome aggregates and adoption notes. Tests not verified as passing.
- This documentation PR stacks on #2; review/merge in order.
- No provider integration, database, authenticated request execution, real benchmark ingestion, deployment or production traffic has been verified.

## P0 — safe foundation (blocker to any billable request)
- Set up GitHub CI: Python 3.11+, pytest, formatting, type/security/dependency checks; verify all tests.
- Authenticate AJ Personal Assistant service and bind server-side tenant/user identity; rate limit and enforce privacy/provider allowlists.
- PostgreSQL budget ledger with idempotent reservation, scoped caps, settlement, lost-response reconciliation and concurrent transaction tests.
- Source-of-truth model/pricing registry, migration framework, audit logs and safe error handling.
- Publish internal versioned API schema and no-PII observability.
Acceptance: synthetic adversarial and concurrency tests pass; fail-closed behavior demonstrated.

## P1 — controlled provider and benchmark staging
- Approved direct OpenAI, DeepSeek and Qwen adapters; credentials only in dedicated staging secret store.
- Optional LiteLLM execution adapter behind independent egress allowlist; benchmark OpenRouter/Portkey only if justified.
- Objective original synthetic benchmark suite with identical tasks, timeout/cost measurements, source provenance and independent scoring.
- Review LiveBench access, license and dataset versions; Artificial Analysis API/terms before ingestion.
Acceptance: no customer prompts; reproducible synthetic measurements, calibrated costs and p95 latency, no unauthorized egress.

## P2 — adaptive intelligence and shadow mode
- Persist versioned outcomes; offline ranking with statistical confidence and task-specific quality floors.
- Replay/shadow comparisons against existing AJ Personal Assistant router, without duplicating paid calls or exposing private content.
- Budgeted, bounded escalation, fallback and circuit breakers; measure total task cost including failures.
Acceptance: non-inferior validated quality/security and measurable cost improvement against defined baseline.

## P3 — AJ integration and rollout
- Integrate authenticated AJ Personal Assistant client behind feature flag; preserve existing router as fallback only where privacy and ledger policy allow.
- Stage load, chaos, budget-race and privacy tests; founder-approved canary; observe cost, quality, errors and rollback.
Acceptance: formal independent review, documented go/no-go and explicit deploy approval.

## P4 — scale internal operations
- Per-tier quotas and authorization workflows, financial reporting, cost anomaly alerts, capacity planning for 1k/10k/100k/1m active-user scenarios.
- No external developer portal, monetized API or GPU self-hosting without new founder decision.

## Questions needing additional specification
- Exact per-tier monetary and token caps, UTC vs local period semantics, and annual renewal rules.
- User-facing approval flow and whether upgrade is possible within existing AJ subscription billing.
- Which query categories are regulated/high stakes and their minimum independently validated quality targets.
- Region/provider allowlists for personal data and contractual retention requirements.
- Latency SLOs by interactive, voice and background task.
- Database choice/plan, staging workspace and approved spending on live synthetic benchmark calls.
