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
