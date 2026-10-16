# AJ LLM Layer — M2 CTO Engineering Completion Report

Date: 16 October 2026 (Europe/London).

## 1. Executive summary

**AMBER overall: PASS for reviewed offline implementation; HOLD for live M1/M3 integration and production certification.** Six stacked draft PRs implement the task-to-model decision pipeline and reproducible synthetic comparison without provider calls. No merges, deployments, Render changes, secrets changes, paid model execution or assistant production routing changes occurred.

The separate reviewer signed off implementation `fcf84bd9ff917774e306121f518aa94f17edc594` after independently executing 100 tests. This is an offline implementation review, not M1 or production certification. A later evidence-only commit packages this report, regenerated schemas, reviewer findings and CI infrastructure. Its SHA and executed local result are recorded in `final-validation.json` alongside the committed report.

## 2. Architecture and module inventory

Ten modules under `aj_llm_layer/routing/` separate metadata classification, complexity, proposed M1 contracts, eligibility, ranking, decimal economics, governed policy, decision records, simulation and demo fixtures. See [architecture](ARCHITECTURE.md) and [integration](INTEGRATION.md). Existing auth, receiver, policy, ranking and disabled execution modules were not changed. The new package has no public endpoint or provider transport.

## 3. Implemented capabilities

Versioned 16-family multi-label taxonomy; explicit unknowns and uncertainty; explainable complexity bands; approved model/provider/tenant/privacy/region/residency/capability/context gates; task/model-bound fresh evaluation evidence; conservative Wilson quality lower bounds; sample and reliability thresholds; expiring versioned prices; budget ceilings; deterministic Pareto/tie ranking; five governed strategies; tenant tightening; approved version rollback; structured no-route; privacy-safe audit records; M1 adapter failure handling; and bounded seeded synthetic workloads. Task models forbid prompts and other unknown fields. JSON schemas are included.

The taxonomy lists initial requirements rather than asserting provider support. Unknown/incomplete task requirements deny routing. Complexity is independent of privacy and does not infer reasoning difficulty solely from tokens. High-stakes financial analysis and explicit high-stakes criticality require stricter approved models and evidence; trusted upstream policy must enrich other domain-specific risk labels.

## 4. Actual executed tests

All runs below used clean `git archive` snapshots, macOS arm64, Python 3.13.5. Commands, environment, exit codes and output are in [test-runs.json](test-runs.json). Each full-suite invocation exited 0, with zero failed and zero skipped tests.

| Exact SHA | Scope | Passed |
|---|---|---:|
| `1f9e1ab979374da6583611aef9a0a8f6e9d4bba0` | Requested frozen baseline | 25 |
| `f4831bb0cfb7fddbe6fb8344adca9488a32d14d3` | Current receiver parent | 28 |
| `02cf3323784ac1213dc23644f95bb438d5eca749` | A | 54 |
| `b6b27c41eaccd24c2300c433620ec4c5d1db71bb` | B | 56 |
| `af92ec02cb7b58edd72327289a50dd8be3a63d6f` | C | 57 |
| `593c911ec066e32a3bc2376b21ca6774d3f264d6` | D | 58 |
| `b6a7028558a6a5677838f4135aba4756d36497e6` | E, intermediate; requires F | 58 |
| `fcf84bd9ff917774e306121f518aa94f17edc594` | F reviewed implementation | 100 |

Coverage includes unit, integration boundary, policy regression, money/cache/retry/rounding, property-style permutations, 200 candidates, three process hash seeds, fault injection, constructed/malformed inputs, unknown sensitive labels and a network-blocked execution test. Receiver/signed authentication tests remain in the full suite. Independent reviewer reran all 100 at the reviewed SHA, exit 0. No claim of live M1 adapter compatibility: only proposed strict contracts are tested. No full assistant suite was run because no assistant file was modified.

GitHub PR checks initially returned an empty rollup; no CI pass is claimed. `m2-offline.yml` adds full-suite/simulation execution and artifacts. Actual post-push check state is recorded separately in final validation; workflow existence is not evidence of execution.

## 5. Security review

Separate review: [independent-review.md](independent-review.md). Fixed findings cover tenant-policy bypass through constructed objects, corrupt snapshots, unknown-label data retention, cross-process hash stability, model/task evidence binding, price expiry, simulation allocation bounds, coherent retry charging and expected success denominator. No unresolved blocking finding in reviewed offline scope. All decisions deny execution authorization. M1 authenticity, atomic reservation, operational snapshot integrity and future authenticated transport remain external review gates.

## 6. Routing policy definitions

Approved enabled immutable versions only. Quality-first orders conservative evidence quality; balanced chooses Pareto candidates then minimizes expected request charges; cost-efficient minimizes expected cost per validated completion; latency-sensitive minimizes p95 subject to hard gates; restricted requires approved restricted-class models, at least .95 quality lower bound, 500 samples and at most .02 failure/timeout sum. Stable ties use model ID after objective factors. Tenant profiles cannot relax platform limits. M1 feature resolution and strategy permissions remain mandatory.

## 7. Economic model

All monetary arithmetic is Decimal. Input/output/cache prices are versioned, expiring synthetic snapshots. For retryable transport probability f and R retries, attempt sum is Σ(f^i), i=0..R. Expected request charges = full attempt estimate × attempt sum. Conservative validated completion probability = min(Wilson lower, 1-f) × attempt sum. Expected cost per completion divides those values. Quality errors terminate rather than retry; all failed and timed-out attempts are charged. Measured cost per success = observed total charges / validated successes. Worst-case retry bound enforces budget eligibility, and eight-decimal ceiling protects rounding. Currency mismatches, missing prices and unknown success economics deny routing. Non-token provider charges need a future explicit contract; v1 cannot authorize those models. M2 never reserves or settles actual funds.

## 8. Simulation results

Reproduce: `python3 -m aj_llm_layer.routing.demo`. [Full report](simulation-report.json) includes seed 42, v1 fixtures, explicit clock, five policy comparisons, simulated failure sensitivity and decision identities. Three invented models/prices and performance evidence are labeled synthetic. Simulation rejects synthetic evidence outside explicit simulation mode. Every simulated actual attempt is charged, including failures, and eligibility first reserves a conservative ceiling within the simulated budget. Latency is an explicitly uncalibrated synthetic distribution. These results do not establish real savings, quality or reliability.

| Strategy | Estimated USD | Simulated successes | Rejected / 100 |
| baseline | 0.985960 | 70 | 30 |
| quality_first | 0.9980255 | 76 | 24 |
| balanced | 0.2111385 | 97 | 0 |
| cost_efficient | 0.2111385 | 97 | 0 |
| latency_sensitive | 0.741408 | 100 | 0 |

## 9. Dependencies on M1 and M4

Receiver #6 remains draft, with frozen baseline preserved. Foundation `policy.eligible_provider` denies all providers. No complete M1 registry/evidence/pricing/budget authorization service was found in inspected receiver code. Proposed contracts and synthetic adapters are explicit rather than permissive production mocks. Assistant staging was fetched at `ec57d34cb43199916fc7551a6a7bdc47dc06e7d1`; its cost guard, provider config and shadow sender were inspected read-only. Existing proposal #604 preserves Cortex policy ownership.

M1 must authenticate and bind immutable snapshots, derive privacy independently of task hints, aggregate all financial limits and reserve funds atomically. M4 must independently validate task/model-version evidence, sample quality, measured charges and latency. `source='independent'` is a trusted adapter assertion rather than proof. M3 must obtain fresh execution/tool authorization and financial reservation independently of this recommendation.

## 10. Outstanding risks

- No real M1 adapter, signed evidence store, deployed budget reservation or authenticated routing endpoint is provided or certified.
- No real provider benchmark, present-day price verification, production performance guarantee or live savings measurement.
- Synchronous adapter deadlines are the adapter's responsibility; this package cannot forcibly cancel arbitrary blocking in-process code.
- Replay requires original immutable snapshots retained outside this pure package. A version string and digest do not archive the evidence.
- Pareto evaluation is quadratic; candidate and simulation-product bounds exist, but maximum-load service objectives are unmeasured.
- Intermediate PRs are dependencies, not individually releasable slices; accept only after final hardening review.
- Local Python 3.13 tests do not prove the entire supported Python/platform matrix. GitHub check state must be read, never inferred.

## 11. Controlled PRs and workstream evidence

Each branch is isolated and every PR is a draft. No merge was performed.

| Workstream / implementation | Branch | Draft PR | Tests | Status |
| M2-A, see architecture module inventory | `codex/m2-a-task-taxonomy` | [8](https://github.com/AJ78607860/AJ-LLM-Layer/pull/8) | 54 passed at `02cf3323784ac1213dc23644f95bb438d5eca749` | PASS offline; integration HOLD |
| M2-B, see architecture module inventory | `codex/m2-b-complexity-contracts` | [9](https://github.com/AJ78607860/AJ-LLM-Layer/pull/9) | 56 passed at `b6b27c41eaccd24c2300c433620ec4c5d1db71bb` | PASS offline; integration HOLD |
| M2-C, see architecture module inventory | `codex/m2-c-eligibility-ranking` | [10](https://github.com/AJ78607860/AJ-LLM-Layer/pull/10) | 57 passed at `af92ec02cb7b58edd72327289a50dd8be3a63d6f` | PASS offline; integration HOLD |
| M2-D, see architecture module inventory | `codex/m2-d-economics-policies` | [11](https://github.com/AJ78607860/AJ-LLM-Layer/pull/11) | 58 passed at `593c911ec066e32a3bc2376b21ca6774d3f264d6` | PASS offline; integration HOLD |
| M2-E, see architecture module inventory | `codex/m2-e-decisions-simulation` | [12](https://github.com/AJ78607860/AJ-LLM-Layer/pull/12) | 58 passed at `b6a7028558a6a5677838f4135aba4756d36497e6` | PASS offline; integration HOLD |
| M2-F, see architecture module inventory | `codex/m2-f-hardening-integration` | [13](https://github.com/AJ78607860/AJ-LLM-Layer/pull/13) | 100 passed at `fcf84bd9ff917774e306121f518aa94f17edc594` | PASS offline; integration HOLD |

Common quality methodology: independent-source/model/task-bound sample evidence, Wilson lower quality gate; synthetic only in simulation. Common economic methodology: versioned Decimal full-attempt charges and bounded retry completion model. Security remediation and residual risks are in section 5 and independent review. Integration dependencies are in section 9; reproducible results are in section 8. No workstream claims production certification.

## 12. Readiness for M3

Typed recommendation contracts, reasons and offline fixtures are available for M3 development. **Live M3 integration: HOLD.** M1/M4 adapters, provenance, budget reservation, fresh authorization and separate review are required. Inference and tool execution remain disabled. Engineering implementation of this offline scope is reviewed; overall M2 integration completion cannot honestly be certified while those dependencies remain unavailable.
