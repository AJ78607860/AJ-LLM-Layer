# Foundation assessment — 2026-10-16

Scope: offline foundation modules in the controlled candidate derived from frozen receiver SHA `1f9e1ab979374da6583611aef9a0a8f6e9d4bba0`. This is a source assessment and synthetic unit-test evidence, not staging certification or production readiness.

## Capability inventory

| Capability | Current implementation and limitation |
| --- | --- |
| Provider-neutral routing | Pure ranking of supplied cost, quality lower-bound and p95 rows. Deterministic cheapest-qualified ordering. No registry, provenance enforcement, automatic switching or live routing. |
| Model evaluation | Two original synthetic fixtures; exact-string and strict JSON objective evaluators. These validate matching only; they are not independent verification of benchmark provenance or a representative performance study. |
| Cost and quality | Offline empirical success counts, Wilson-style lower bound and total-cost/success. Caller provides outcomes; validation flag is not cryptographic evidence. In-memory mutable dictionaries are test scaffolding, not a concurrent/persistent production ledger. |
| Privacy and classification | All contexts classified personal; all providers denied. No authenticated tenant-policy store or content-bearing inference endpoint is authorized by these modules. Metadata-only endpoint security is assessed separately. |
| Execution and reliability | Adapter protocol and disabled implementation only. No provider implementation, timeout enforcement, retries, execution circuit breaker or backpressure for inference. |
| Observability | No inference execution events exist. Receiver correlation and metadata diagnostics are separate concerns; cost/quality audit trails and alerting remain future work. |
| Configuration | No feature enables provider execution in these modules. Enabling real execution requires a separately reviewed implementation and explicit approval. |
| Testing/release | Synthetic tests exercise offline behavior and failure cases. Full receiver, sender-contract, concurrency, security and staging results must be reported separately. |

## Candidate changes

- Reject NaN, infinity, boolean, non-numeric and unrepresentably large routing requirements; exclude candidates carrying these invalid numeric values. Empty/invalid provider/model identities cannot enter ranking.
- Validate independently validated outcomes using strict booleans, finite nonnegative cost/latency and nonempty identities before mutation. Reject aggregate cost overflow before changing any statistics.
- Bound evaluator strings to 65,536 characters; reject duplicate JSON keys, non-standard NaN/Infinity, numeric exponent overflow, excessive nesting, and JSON boolean/number type confusion. JSON object key order remains irrelevant. JSON numeric types are deliberately strict (`1` differs from `1.0`); fixtures must select the expected representation explicitly.
- Preserve disabled adapter and deny-all provider policy. Nothing calls a provider or enables online learning.

Targeted executed command from candidate directory:

```text
../aj-llm-layer-1f9e1ab/clean-runner/bin/python -m pytest -q tests/test_foundation.py tests/test_foundation_hardening.py
35 passed in 0.03s (exit 0)
```

The isolated runner's editable installation references the frozen source tree; running from the candidate directory ensures candidate imports. These are working-tree results, not claims about a newly committed SHA. Root-level runs without candidate import selection do not certify candidate behavior.

## Sequenced backlog

### P0: before any content-bearing or billable execution

1. Versioned registry and evidence provenance with immutable fixture/outcome IDs, approved task-specific quality floors and measured total cost including failures.
2. Authenticated tenant and server-derived privacy policy; provider/region/retention allowlists and negative tenant-isolation tests. The current all-personal/deny-all policy remains until then.
3. Transactional persistent budget ledger: idempotent reservations, caps, settlement, lost-response reconciliation and concurrent race tests. Define currency precision/rounding explicitly.
4. Reviewed bounded provider adapters with enforced timeouts, retries, backpressure, egress allowlists and separately approved synthetic staging credentials. No default adapters.
5. Persisted outcome validation, retention, audit provenance and concurrency semantics. Bound identity cardinality. Current in-memory statistics must never be exposed directly to untrusted callers or described as production learning.
6. Dependency audit, reproducible exact-commit tests, independent security review and separately approved staging tests; security gates for the receiver/sender remain mandatory.

### P1: controlled synthetic measurement

1. Representative, original synthetic task corpus, independently defined expected outcomes, versioned evaluator semantics and reproducible run manifests.
2. Calibrate cost/reliability/p95 estimates with minimum sample sizes and uncertainty, including all failed attempts. Caller-supplied metrics remain untrusted evidence.
3. Safe structured execution metrics and actionable alerts without prompts, user identity, responses or credentials; bounded labels and retention.
4. Load/chaos testing of bounded retries, circuit breakers, fail-closed policy and billing reconciliation.

### P2: separately authorized intelligence

1. Offline ranking comparisons and shadow analysis using approved metadata only, with explicit quality/cost baselines.
2. Budgeted fallback/escalation with policy and ledger gates at every attempt.
3. Versioned offline model promotion and rollback; automatic switching/online learning remain disabled pending an explicit new design and release review.

No provider, infrastructure or spending selection was made. Larger capabilities are deliberately specified as backlog rather than activated in this candidate.
