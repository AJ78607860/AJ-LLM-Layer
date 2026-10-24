# AJ inference strategy and certification plan

Date: 24 October 2026
Status: Owner-approved direction; implementation and certification pending.
Release: HOLD. No merge, deployment, provider provisioning, paid capacity commitment or production activation authorised by this document.

## Decision
Buy managed inference initially. Extend AJ-LLM-Layer rather than introduce a parallel router or runtime. AJ owns context selection, privacy policy, orchestration, authorisation, execution and verification. Do not train a foundation model. Evaluate dedicated or self-hosted open-weight inference only against measured economics or specific privacy requirements.

This is a plan, not evidence that these capabilities are implemented. Repository README confirms intended scope only; a source audit remains required.

## Architecture and ownership
- Sovereign supplies authenticated tenant identity, processing scopes, data classification and allowed provider/model/region policy.
- Context Intelligence supplies minimal relevant context with provenance and deletion/revocation constraints.
- Cortex owns task planning and workflow recovery.
- LLM Layer owns approved model selection, provider adapters, capability compatibility, budgets, timeouts, circuit state and inference telemetry.
- Action Gateway validates and executes actions, including approvals and idempotency.
- Mission Control displays verified state and outcomes.

Preserve existing ownership boundaries. Audit existing implementations and interfaces in both repositories before code changes; record reuse and gaps. Account identity must come from authenticated context, not model-supplied fields.

## Execution policy
1. Use deterministic software for exact lookups, calculations, permission checks and receipt verification.
2. Use an economical qualified model for bounded extraction, classification and routine summarisation.
3. Use a qualified reasoning model when complex planning or ambiguity requires it.
4. Validate structured outputs before use; model output never grants permission.
5. Escalation and fallback must remain within provider, region, modality, deadline and budget constraints. Fail closed when no eligible route exists.
6. Personal data remains on the approved OpenAI route until separately approved. Synthetic-only alternative-provider evaluations must contain no production-derived personal data. Shadow calls also require policy and budget checks.

## Work packages
### A: Establish baseline
Audit source, existing tests, configuration and integration paths; inventory provider adapters, routing ownership and outstanding certification. Measure tokens, calls, latency, errors and total cost per completed task. Record current gaps without claiming readiness.

### B: Reduce unnecessary inference
Apply relevant-tool selection and bounded retrieval. Reuse resolved contacts within authenticated workflow scope; invalidate on account changes, logout, revocation and expiry. Avoid repeated planning for deterministic actions. Isolate caches by tenant and policy; prohibit cross-account reuse.

### C: Harden the existing routing interface
Use an internal request contract carrying authenticated tenant, task identifier, workload class, data class, provenance references, eligible providers/models/regions, token limits, deadline and remaining budget. Do not trust client overrides.
Return a validated result or typed refusal/error with provider/model/version, usage, cost basis and correlation identifier.
Reserve estimated cost before calls, bound input and output, reconcile actual usage, and account for retries, fallback and shadow calls. Mark missing usage as unknown and handle conservatively. Keep model/tool accounting distinct while enforcing an overall task budget.
Avoid raw prompts and personal content in routine logs.

### D: Synthetic benchmark
Compare the existing approved route with one candidate supplier selected after capability, privacy and commercial review. Use identical fixtures and record model version, serving configuration, date and parameters. Do not rank suppliers solely by tokens/second.
Score task success, valid tool arguments, extraction accuracy, refusal correctness, time to first token, total and tail latency, retries and cost per successful task. Include provider outages and schema incompatibilities.

### E: Controlled adoption
No automatic activation. Retain HOLD until security, privacy and reliability gates pass and explicit release approval is received.
Consider private inference only after a model passes quality tests and total operating cost includes idle capacity, redundancy, engineering, security and maintenance. Serving existing licensed weights is distinct from training.

## Acceptance gates
- Existing source and interface audit identifies reused components; no duplicate router/runtime.
- Exact deterministic fixture completes without a model call.
- Personal-data request cannot route or fail over to an unapproved processor.
- Synthetic routing honours model capability and regional constraints.
- Tenant A context, resolved contacts and cache entries never appear for tenant B.
- Logout, revocation and expiry invalidate scoped reusable state and block subsequent unauthorised use.
- Oversized context is bounded or refused before a supplier request.
- Retries, escalation and shadow calls cannot exceed the reserved overall budget; unknown usage is not treated as free.
- Malformed tool arguments are rejected; inference cannot bypass Action Gateway approval or idempotency.
- Timeout and circuit-breaker paths produce explicit recoverable state without repeating an external action.
- Routine logs exclude prompt bodies and personal content; provenance and usage metadata remain traceable.
- Failed, missing or incomplete benchmark/test evidence blocks certification.

Define numerical quality and latency thresholds from the baseline before comparing candidates. Targeted tests precede integration and full regression checks. Report exact commits, commands, fixtures, results and blockers; synthetic evidence does not certify live IAM, retention or regional controls.

## Commercial decision
No supplier winner selected. Compare contract terms, retention/training policy, subprocessors, residency, deletion, availability, tool compatibility and workload performance before approval. Evaluate API and dedicated costs using actual volume and utilisation; user count alone is insufficient.

## Evidence and sequencing
This work must not weaken or obscure the existing Cloud Build recovery gate. Keep planning isolated from recovery changes. Before implementation, reconcile the latest application and LLM-Layer heads and applicable repository instructions. No claim of implementation, passing tests or production readiness follows from approval of this strategy.
