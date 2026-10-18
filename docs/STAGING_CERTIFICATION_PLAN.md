# Controlled staging certification plan

Status: preparation only. **Do not merge or deploy without the owner's explicit
approval.** No production inference, customer shadow traffic, secrets or live
infrastructure changes are authorized by this candidate.

## Verified infrastructure inventory (16 October 2026, read-only)

Workspace: AJ Personal Assistant (`tea-dat840jtqb8s73a5aqi0`).

| Service | Service ID | Current live commit | Deploy ID | Auto-deploy |
| --- | --- | --- | --- | --- |
| aj-llm-layer-staging | srv-db83qjij9qps73bhkck0 | 9eb10992ba22691e515c79359c862b2a57fd1c7e | dep-db847k689jmc73c52g4g | off |
| aj-staging | srv-datimeqd0e5s73c92de0 | ec57d34cb43199916fc7551a6a7bdc47dc06e7d1 | dep-db8mo3m89jmc73eoingg | off |

Receiver service tracks `feat/internal-shadow-bridge`; AJ tracks `release/staging`.
These are deployment metadata observations, not live integration test evidence.
No environment-variable values, logs or customer traffic were read. Candidate
branch/commit/PR identities are in the engineering handoff, not deployed here.

## Prerequisites before requesting deployment approval

1. Exact candidate SHAs and archived source checksums recorded; frozen baseline
   kept unchanged. Receiver and AJ full suites plus mandatory checks must pass.
   Required GitHub checks remain authoritative; local tests do not override them.
2. Independent R3 review reconciled to exact candidate source, with all Major
   findings fixed or explicitly risk-accepted by the owner.
3. Authenticated TLS shared replay store selected and owner-approved separately.
   Verify noeviction, durable acknowledged writes, all instances sharing one
   namespace, restricted ACL, capacity monitoring and state-loss quarantine.
   No provisioning or paid infrastructure action is part of this plan.
4. Staging-only dummy/synthetic credentials generated through an approved secret
   channel. Never copy real production keys. Do not commit or log secret values.
5. Both services select `AJ_LLM_LAYER_AUTH_MODE=signed`, with matching environment
   and distinct staging-only signing material. Receiver replay backend is redis;
   local backend must be rejected on staging. Keep AJ shadow feature OFF by
   default and invoke only the approved synthetic test harness. Do not turn on
   the app-wide observation hook for customer turns.
6. Rollback deploy identities above revalidated immediately before approval.
   Archive current configuration through approved secure operations, never chat.
7. Confirm edge rate/connection limits and a controlled synthetic concurrency
   budget. Confirm DNS-pinned HTTPS transport and no redirected/proxied egress.

Only when these prerequisites are satisfied, request approval naming **both
candidate SHAs, both services, configuration names and the synthetic test scope**.
Approval to deploy does not imply permission to enable customer or inference traffic.

## Approved-run test matrix

Capture UTC timestamps, source SHAs, deployment IDs, synthetic correlation IDs,
HTTP status/counts and redacted fixed-outcome logs for each row:

| Area | Expected result |
| --- | --- |
| Fresh AJ-signed metadata | 200; correlation echoed; shadow_only; no providers; no billable execution |
| Wrong key/environment/audience/scope/method/path/body | 401; no schema or secret leakage |
| Missing/malformed/oversized credential | 401; no fallback |
| Expired at exp and exp+1; future iat+31 | 401 |
| Valid iat+30 skew, 60-second max lifetime | accepted only before strict expiry |
| Replay after acceptance, across two workers/instances | exactly one 200; all repeats 401 |
| Receiver restart with durable store | accepted nonce remains rejected |
| Store unavailable/full | fixed 503; no local/static fallback |
| Prompt/user/response/unknown task fields | 422 after auth; unauthenticated request 401 |
| Chunked oversized/slow body | 413/408; bounded resource use |
| Invalid correlation | generated replacement; no unsafe value logged |
| Receiver down/timeout/invalid response | AJ false result; breaker opens; existing routing unaffected |
| Key rotation | new kid accepted; old retiring kid only until deliberate removal; nonce not reset |
| Logs/metrics/no-egress | no credentials/content; fixed labels; zero model/provider/billing calls |

Record failures as failures, preserve redacted diagnostics and halt the synthetic
run on unexpected acceptance, sensitive logging, provider egress or user impact.

## Rollback

Stop the synthetic harness first; keep shadow observation and inference OFF.
Owner-approved operations restore AJ deployment `dep-db8mo3m89jmc73eoingg`
and receiver `dep-db847k689jmc73c52g4g` (or their newly captured pre-run versions).
Restore staging-only configuration from its secure snapshot. Do not restore a
replay snapshot and resume old keys without the 120-second quarantine/new-key
procedure. Verify health/readiness and existing synthetic AJ journeys, then
record actual rollback deployment IDs and commit identities.

Live integration certification requires an additional report after this approved
run. Production readiness is a separate later decision.
