# End-to-end synthetic staging certification (M3 slice)

Status (19 Oct 2026): **implemented and tested locally across both services. Not deployed. Not verified on
staging.** Production provider switching stays off; the certification code cannot run in production.

## What this certifies

One operational path, end to end, with evidence:

```
AJ (synthetic fixture)
  └─ Cortex classifies            core.cortex.understanding + model_policy (real code, no model call)
      └─ signed POST /internal/v1/certification/decide   (metadata only: class, tier, token estimates)
          └─ Layer: M2 routing over the approved registry  -> RECOMMENDATION or evidence-bootstrap probe
             Layer: atomic reservation of worst-case cost  -> single-use grant
                    (model, max input/output tokens, max attempts, per-attempt timeout, expiry)
      └─ Cortex policy gate (AJ): provider allowed for the data class, Sovereign/egress checks,
         AJ model allowlist, AJ bounds. Refusal => outcome "refused_by_policy", hold released.
      └─ AJ executes through its existing OpenAI client, inside the grant only; AJ cost guard first
      └─ deterministic validator (AJ)
      └─ signed POST /internal/v1/certification/outcome  (usage counts, latency, error class, pass/fail)
          └─ Layer: charge actual cost (unknown usage at the bound), release remainder,
             record evidence (synthetic), update per-model circuit breaker
```

Ownership is unchanged from the 15 Oct split (AJ #604, ADR-042): **the Layer recommends, budgets, owns
provider health and learns; Cortex decides and AJ executes.** The Layer never holds a provider key, never
executes inference and never receives prompts or responses.

## Interfaces

| Route | Scope | Body | Notes |
|---|---|---|---|
| `POST /internal/v1/certification/decide` | `certify:decide` | `aj.cert.decide.v1` | 503 when disabled; returns decision record + grant or refusal |
| `POST /internal/v1/certification/outcome` | `certify:outcome` | `aj.cert.outcome.v1` | single use per grant; accepted while disabled so holds settle |
| `POST /internal/v1/certification/status` | `certify:read` | empty | budget, evidence, breakers; nothing written except sweeping expired holds |

All three: signed `X-AJ-Service-Auth` only (static bearer refused), shared replay store, 4 KiB body cap,
strict schemas (unknown fields 422), **404 unless `AJ_ENVIRONMENT` is staging/development/local/test**.

## Safety properties (each covered by a test)

- Default daily budget is 0: no grant can ever be issued until a budget is configured. Code ceilings:
  $5/day, $0.50/run, 4096 output tokens, 8 concurrent holds; env cannot raise them.
- Ledger unconfigured or unwritable: 503, nothing executed (founder decision 8).
- Reservations are atomic (SQLite `BEGIN IMMEDIATE`); 12 concurrent reservers never exceed the cap.
- Grants outlive every permitted attempt (`max_attempts × timeout + 30 s`); abandoned grants are charged their
  full hold, never zero.
- Contract violations (extra attempts, token overruns, overspend) are charged but never become routing
  evidence and never move the circuit breaker.
- High-stakes and financial-analysis tasks are refused (no restricted approval exists).
- Evidence is `source=synthetic` and only accepted in certification; it cannot leak into customer routing.
- Provider health: 3 consecutive transport failures open a 5-minute circuit; quality failures do not.
- AJ side: never imported by the reply path; refuses while `AJ_ROUTING_POLICY_MODE=on`; production routing
  fingerprint (core model chain, side-task chains, routing flags) is recorded before and after every run.

## Local two-service evidence (19 Oct 2026)

Real Layer service (uvicorn, SQLite ledger), real AJ harness (Cortex, signer, policy gate, OpenAI SDK),
**fake OpenAI Responses server** on loopback. Run 4, final code (after the independent review fixes), fresh
ledger, `--repeat 5 --include-slow --local-fault-drills`, AJ allowlist = all three OpenAI tiers:

| Area | Result |
|---|---|
| 20 executing fixture runs (4 fixtures × 5) | 20/20 validated, all bootstrap probes on gpt-5.6-luna (evidence is per family: 15 reasoning + 5 writing, so none reached the 20-sample route threshold; graduation is covered by unit test) |
| Payment fixture | Cortex `safety_critical/strong` → Layer `HIGH_STAKES_RESTRICTED`, no grant, no execution |
| Kill switch / production refusal | refused before any network call |
| Layer unreachable | fail closed, nothing executed |
| Cortex policy refusal | outcome `refused_by_policy`, charged 0, full release |
| Replay | same signed request: `[200, 401]` |
| Abandoned grant | late outcome `409 GRANT_EXPIRED`, full hold charged |
| Wrong answer | counted as a failed sample; breaker untouched; luna still probe-eligible afterwards |
| Provider 5xx ×3 | circuit opened on luna; next decision rerouted to terra and validated |
| Provider timeout | bounded at the grant's 30 s; unknown usage charged at the bound; overspend 0 |
| Production routing fingerprint | unchanged |
| Ledger spend | $0.0040 (virtual: fake provider) |

Runs 1–3 (same day, before the fixes) are what found the grant-TTL bug and the over-narrow drill checks.

## Independent review (separate agent, 19 Oct)

Verdict **PASS WITH FINDINGS**, no Critical/High. Fixed with regression tests: M1 (cached-token estimate could
shrink the hold; overspend flag now persisted and excluded from evidence), M2 (AJ allowlist is now
provider/model pairs; executor provider must be openai), M3 (missing usage is "unknown", charged at the bound),
M4 (evidence per task family), M5 (capped bootstrap below 60 samples), L1, L2, L3, L4, L6, L8, L10.
Open by decision: **M6** ledger durability / multi-replica (see D-B), L5 (holds near London midnight count toward
the earlier day), L7 (no decision-table retention or per-caller rate limit), L11 (no half-open breaker state),
L12 (certification and shadow share signing keys; scopes separate them logically only).

Observations worth acting on:
1. **Small-sample sensitivity is real.** One wrong answer at ~20 samples pulls a 95% Wilson lower bound below
   0.80. Budget ~50+ samples per model per family before reading anything into a switch.
2. **Cortex wording quirks** (found by classifying the fixtures, not changed here): an instruction containing
   "reply" is classified `EXTERNAL_WRITE` → `tool_orchestration`; "Pay £50 … now" gets intent `news_lookup`
   (risk class is still `PAYMENT`, so the safety outcome is right). For Cortex owner review.
3. AJ's cost guard refused the first local attempt (run as root → data-root safety → kill switch treated as on).
   The abandoned hold from that crash was later charged in full by the sweeper, as designed.

## Staging run: state and remaining steps (updated 19 Oct, 21:30)

Decisions D-A/D-B/D-C approved by the owner (19 Oct, 16:54): free Key Value for replay, ephemeral ledger
accepted for synthetic runs, $0.25/day and $0.02/run.

Done (verified read-only on Render):
- `aj-llm-layer-staging` tracks `main`, live on `a6152e0` (#14); clean startup.
- `aj-staging` live on a commit containing AJ #679.
- Key Value `aj-llm-replay-staging` (`red-dbb3s0igjjms73ctmhsg`): free, Frankfurt, `noeviction`, persistence
  off, inbound allow-list = the Layer's two outbound ranges `74.220.51.0/24`, `74.220.59.0/24` only.

Found and fixed after the merge (follow-up PRs): AJ refused every bootstrap probe because a later hardening
commit expected a `PROBE` status the Layer never sends; contract tests now replay real Layer responses. The
free-plan Layer sleeps after 15 minutes idle; the runner now wakes it first.

Remaining, in order:
1. Merge the follow-ups (Layer diagnostics PR; AJ probe-gate/preflight PR) and deploy both services manually.
2. Layer → Environment. `AJ_LLM_LAYER_SIGNING_KEYS` **already existed** on the Layer before this work: keep it
   unless the owner decides otherwise, and make aj-staging hold the same `kid:key` pair. Set:
   `AJ_ENVIRONMENT=staging`, `AJ_LLM_LAYER_REPLAY_BACKEND=redis`, `AJ_LLM_LAYER_REPLAY_REDIS_URL` (External
   `rediss://` URL), `AJ_LLM_LAYER_CERTIFICATION_ENABLED=true`, `AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD=0.25`,
   `AJ_LLM_LAYER_CERT_MAX_RUN_USD=0.02`, `AJ_LLM_LAYER_CERT_LEDGER_PATH=/tmp/aj-cert-ledger.sqlite3`.
   The startup log line `certification_config` must then show `"ready": true` and the expected key ID(s).
3. aj-staging → Environment: `AJ_LLM_LAYER_SIGNING_KEYS` (same pair) and `AJ_LLM_LAYER_SIGNING_KID` (its kid).
   Leave `AJ_LLM_LAYER_SHADOW_ENABLED` off.
4. aj-staging Shell, preflight (no execution, no spend; prints named blockers):
   ```
   AJ_LLM_LAYER_CERTIFICATION_ENABLED=true AJ_LLM_LAYER_AUTH_MODE=signed \
   AJ_LLM_LAYER_CERT_URL=https://aj-llm-layer-staging.onrender.com \
   ops/bin/aj python3 /app/scripts/llm_layer_certification_run.py --preflight
   ```
5. Same prefix, the run:
   ```
   ... ops/bin/aj python3 /app/scripts/llm_layer_certification_run.py --repeat 5 --include-slow \
     --out /var/data/llm-layer-cert/run-$(date -u +%Y%m%dT%H%M%SZ)
   ```
   Exit 0 = every fixture and drill met expectation.

Go / no-go for "verified on staging": exit 0; production fingerprint unchanged; replay `[200, 401]`;
ledger `overspend` 0 on every outcome; no content in Layer logs; spend within cap.

## Rollback

The flag is only set for the one command, so the AJ side is off as soon as it exits. Layer: set
`AJ_LLM_LAYER_CERTIFICATION_ENABLED=false` (new decisions 503, outstanding outcomes still settle), or roll back
to `dep-db847k689jmc73c52g4g`. AJ: no routing behaviour changed, so nothing to roll back; remove the signing
pair if desired. Customer traffic never touches this path.

## Not in scope / still gated

Production provider switching, customer traffic, non-OpenAI providers for personal data, durable ledger,
per-family evidence granularity, Layer health snapshot consumption by Cortex (#604 Stage 1–2).
