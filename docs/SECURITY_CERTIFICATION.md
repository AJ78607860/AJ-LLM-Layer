# Shadow receiver security contract

This candidate is metadata-only. It exposes no inference API, registers no live
adapter and authorizes no provider. Offline ranking, evaluation and statistics
are disconnected from request execution. No user/tenant content endpoint exists;
tenant-authenticated execution and budgets remain future release gates.

## Authentication and expiry

Signed mode is required for certification. Static and both are compatibility
modes only: static credentials have neither single-use nor expiry guarantees.
Invalid modes fail closed. Neither signed-only nor invalid signed credentials in
both mode fall back to static. Static Bearer syntax and ASCII are mandatory.

HMAC-SHA256 binds issuer, audience, environment, scope, POST path and SHA-256
of the exact body bytes. Constant-time comparisons protect signatures/hashes.
Credential headers are ASCII and at most 1024 characters. Duplicate auth headers,
queries and encoded path aliases are rejected. Claims have an exact field set,
integer timestamps (not booleans), base64url nonce/hash and no duplicate JSON keys.
Signing keys are ASCII 32–256 characters; malformed or duplicate key configuration
invalidates the entire key set. Rotation accepts multiple distinct key IDs.

Lifetime is 1–60 seconds. A future-issued credential may be at most 30 seconds
ahead of the receiver. Expiry is strict: receiver time **must be less than exp**.
There is no expiry grace period. Shared Redis time independently enforces exp
inside atomic insertion, and rejects implausible expiry >90 seconds ahead.
The old exp+30 acceptance contract is intentionally tightened; sender reference
verification must match, while existing compliant sender signatures remain valid.

## Distributed replay

`AJ_LLM_LAYER_REPLAY_BACKEND=redis` is the default. Configure
`AJ_LLM_LAYER_REPLAY_REDIS_URL` with authenticated rediss/TLS in staging and
production. Every receiver worker/instance in an environment must use the same
store/database and namespace. Cross-environment keys must be distinct.

A Lua transaction checks Redis time, prunes expired entries, rejects duplicates,
enforces the shared 10,000-nonce capacity and inserts a SHA-256 nonce digest.
No credential, body, key or raw nonce is stored. Entries are retained through
absolute exp+30 on the shared clock, and the registry key has a 180-second idle
TTL (greater than maximum nonce retention). Key rotation does not reset replay.
Socket/connect timeout is 0.5 seconds; pool size is capped at 16. Capacity or
store failures return a fixed 503 and never fall back to memory/static auth.

Local memory requires explicit backend `local` and environment `test`, `local`
or `development`. It is not a distributed-safe or restart-safe backend.

Deployment prerequisites: dedicated non-evicting Redis/compatible store,
TLS certificate verification, least-privilege ACL, capacity alerting, durable
acknowledgement and a verified persistence/failover policy. Replication with
acknowledged-write loss is insufficient for strict single-use guarantees.
After state loss/restore, stop admission for at least 120 seconds with healthy
clocks, or rotate to entirely new signing material before resuming. Otherwise
previously accepted tokens could be accepted again. These are live gates, not
properties established by local code tests. No Redis resource was provisioned.

## Resource and privacy boundaries

The endpoint reads at most 1024 bytes, including chunked uploads, with a two-second
body deadline. Sixteen requests are admitted per process; excess returns 503.
Blocking store operations run outside the event loop. Edge-level rate limiting
and aggregate multi-instance load/latency tests remain staging prerequisites.

Task names are a fixed eleven-label allowlist matched to AJ. Token counts are
strict bounded integers. Extra fields, malformed/duplicate metadata and prompt
fields are forbidden. Authentication precedes schema validation. Fixed error
messages omit submitted values, key configuration and exception details.

Correlation headers retain the existing safe ASCII 8–64-character contract and
are echoed on success. Invalid IDs are replaced. Logs contain fixed outcomes and
correlation IDs only; counters have fixed labels and no high-cardinality input.
AJ must supply generated RouteTrace identifiers, never customer-derived IDs.
Syntax cannot prove the semantics of an authorized caller's identifier or count:
this boundary prevents content-bearing fields, not malicious covert channels.

## Executed evidence and limits

Frozen baseline: 25 tests passed after a fresh declared-dependency installation.
Candidate tests include adversarial auth/time/config/schema cases, atomic shared
Redis races, separate processes, persisted Redis restart and unavailable-store
failure. Actual AJ source is exercised through a substituted HTTP boundary,
using dummy credentials and the identical committed golden vector.

See the separately delivered exact-SHA report for final counts, versions and
independent review. In-process HTTP substitution does not certify Render TLS,
DNS, deployment configuration, live logs or provider no-egress infrastructure.
Production and live staging integration are not certified by this document.
