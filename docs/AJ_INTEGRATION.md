# AJ Personal Assistant -> AJ LLM Layer: shadow bridge

Initial integration is metadata-only and strictly **disabled by default**. It does not replace AJ's authoritative router or execute inference.

## Contract
POST /internal/v1/shadow
Authorization: Bearer <long randomly generated service token>
Content-Type: application/json

{"task_family":"light_text","estimated_input_tokens":100,"estimated_output_tokens":50}

Response: {"mode":"shadow_only","task_family":"light_text","data_class":"personal","eligible_providers":[],"billable_execution":false}

No prompt text, user identity, conversation metadata or private content may be sent. Unknown JSON fields are rejected. Configure AJ_LLM_LAYER_SHADOW_TOKEN independently in each dedicated service's secret store, and configure AJ_LLM_LAYER_SHADOW_URL only in AJ Personal Assistant after staging deployment and approval. Use HTTPS and a trusted fixed URL; never derive URL from requests.

## Important limits
Bearer auth alone does not provide replay protection, tenant binding or end-to-end service identity. This endpoint is *not* a production inference API. It must not be exposed publicly for actual inference. Add mTLS or short-lived signed service credentials, replay protection, per-client rate limits, atomic budget ledger and provider authorization before production execution.

## Rollout
1. Merge foundation PRs in order after CI/review.
2. Deploy standalone staging service after approval; keep token disabled until both sides configured.
3. Enable shadow mode in AJ staging only and verify no prompts leave the router.
4. Measure metadata-only shadow errors/latency, then plan authenticated budgeted inference API.
5. No automatic cutover to AJ LLM Layer; current AJ router remains in place.

## Signed receiver candidate (not activated)

The receiver implementation in this branch mirrors the AJ signer at
`llm_router/service_auth.py`. A request in signed mode uses
`X-AJ-Service-Auth: v1.<kid>.<claims>.<mac>`, scope `shadow:write`,
issuer `aj-assistant`, audience `aj-llm-layer`, exact POST path and
body hash. Claims include environment, issue/expiry timestamps and a
single-use nonce. Expiry is capped at 60 seconds with 30 seconds clock skew.

Set `AJ_LLM_LAYER_AUTH_MODE=signed` **on the receiver only after tests**,
with `AJ_LLM_LAYER_SIGNING_KEYS` (matching AJ's signer) and
`AJ_ENVIRONMENT=staging`. The receiver defaults to `static` for
compatibility. `both` is a temporary migration mode: signed headers are
always verified and never silently downgraded to static.

The optional `X-AJ-Correlation-ID` is accepted only if it matches
ASCII `[A-Za-z0-9_-]{8,64}`; otherwise the receiver generates a
`llm_<32 hex>` ID. It is returned in the response header and logged
without request content. It is not part of the signed body.

**Security limitation:** Replay protection is process-local and lost
across restarts. Do not use multiworker/multireplica receivers or enable
production until a shared atomic replay store with TTL and capacity
limits is implemented and tested. Do not enable model execution,
provider routing or production. Never put real signing keys in tests or
GitHub. Deploy only after independent review and explicit staging
authorization; verify signed success, replay, expiry, wrong body,
wrong environment and correlation against AJ's exact deployed SHA.
