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
- Contract violations (extra attempts, token overruns) are charged but never become routing evidence.
- High-stakes and financial-analysis tasks are refused (no restricted approval exists).
- Evidence is `source=synthetic` and only accepted in certification; it cannot leak into customer routing.
- Provider health: 3 consecutive transport failures open a 5-minute circuit; quality failures do not.
- AJ side: never imported by the reply path; refuses while `AJ_ROUTING_POLICY_MODE=on`; production routing
  fingerprint (core model chain, side-task chains, routing flags) is recorded before and after every run.

## Local two-service evidence (19 Oct 2026)

Real Layer service (uvicorn, SQLite ledger), real AJ harness (Cortex, signer, policy gate, OpenAI SDK),
**fake OpenAI Responses server** on loopback. Run 3, fresh ledger:

| Area | Result |
|---|---|
| 20 executing fixture runs (4 fixtures × 5) | 20/20 validated; first 20 were bootstrap probes on gpt-5.6-luna |
| Graduation | at 20 validated samples luna became an evidence-based `RECOMMENDATION` (2 attempts allowed) |
| Payment fixture | Cortex `safety_critical/strong` → Layer `HIGH_STAKES_RESTRICTED`, no grant, no execution |
| Kill switch / production refusal | refused before any network call |
| Layer unreachable | fail closed, nothing executed |
| Cortex policy refusal | outcome `refused_by_policy`, charged 0, full release |
| Replay | same signed request: `[200, 401]` |
| Abandoned grant | late outcome `409 GRANT_EXPIRED`, full hold charged |
| Wrong answer | counted as failed sample; breaker untouched |
| Provider 5xx ×3 | circuit opened on the failing model; next decision rerouted around it and validated |
| Provider timeout | bounded at the grant's 30 s; unknown usage charged at bound; overspend 0 |
| Production routing fingerprint | unchanged |
| Ledger spend | $0.0152 (virtual: fake provider) |

Observations worth acting on:
1. **Small-sample sensitivity is real.** One wrong answer at 21 samples drops luna's 95% Wilson lower bound to
   0.77 (< 0.80 floor), so routing moves to the next model. Correct statistically; budget ~50+ samples per model
   before reading anything into a switch.
2. **Cortex wording quirks** (found by classifying the fixtures, not changed here): an instruction containing
   "reply" is classified `EXTERNAL_WRITE` → `tool_orchestration`; "Pay £50 … now" gets intent `news_lookup`
   (risk class is still `PAYMENT`, so the safety outcome is right). For Cortex owner review.
3. The first local attempt ran as root and AJ's cost guard refused it (data-root safety → kill switch treated
   as on). The abandoned hold from that crash was later charged in full by the sweeper, as designed.

## Staging run: what is needed (owner actions; nothing here was changed)

Verified read-only on 19 Oct: `aj-llm-layer-staging` is **free plan**, branch `feat/internal-shadow-bridge`,
live deploy `dep-db847k689jmc73c52g4g` (commit `9eb10992`), no disk; **no Key Value instance exists**.

Decisions only the owner can make:
- **D-A Replay store.** Staging signed auth requires `rediss://` with a password (local replay is refused on
  staging). Recommended: one Render Key Value instance for synthetic staging, used via its TLS external URL.
  Free Key Value is not durable: after a restart, apply the documented 120-second quarantine (rotate the kid).
- **D-B Ledger durability.** Free plan has no disk, so the SQLite ledger resets on restart/spin-down.
  Recommended for this run: accept (tiny caps; AJ's cost guard and the OpenAI "AJ Dev" project limit are the
  backstops). A durable ledger (disk or Postgres) is required before any non-synthetic use.
- **D-C Budget.** Recommended `AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD=0.25`, `AJ_LLM_LAYER_CERT_MAX_RUN_USD=0.02`.
  A full run (`--repeat 5`) is ~21 calls on gpt-5.6-luna, a few cents at the (unverified) table prices.

Then, in order:
1. Merge the two PRs (Layer integration PR into `main`; AJ harness PR into `release/staging`).
2. Point `aj-llm-layer-staging` at `main` and deploy manually. Set on the Layer only:
   `AJ_ENVIRONMENT=staging`, `AJ_LLM_LAYER_SIGNING_KEYS`, `AJ_LLM_LAYER_REPLAY_BACKEND=redis`,
   `AJ_LLM_LAYER_REPLAY_REDIS_URL`, `AJ_LLM_LAYER_CERTIFICATION_ENABLED=true`,
   `AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD`, `AJ_LLM_LAYER_CERT_MAX_RUN_USD`,
   `AJ_LLM_LAYER_CERT_LEDGER_PATH=/tmp/aj-cert-ledger.sqlite3`.
3. On `aj-staging` set the staging-only signing pair `AJ_LLM_LAYER_SIGNING_KEYS` + `AJ_LLM_LAYER_SIGNING_KID`
   (same kid:key as the Layer). Do **not** set `AJ_LLM_LAYER_CERTIFICATION_ENABLED` on the service, and do not
   change `AJ_LLM_LAYER_SHADOW_ENABLED` (stays off). Deploy release/staging manually.
4. Run from the aj-staging Shell (flag set only for this one command, so it is off again afterwards):
   ```
   AJ_LLM_LAYER_CERTIFICATION_ENABLED=true AJ_LLM_LAYER_AUTH_MODE=signed \
   AJ_LLM_LAYER_CERT_URL=https://aj-llm-layer-staging.onrender.com \
   ops/bin/aj python3 /app/scripts/llm_layer_certification_run.py --repeat 5 --include-slow \
     --out /var/data/llm-layer-cert/run-$(date -u +%Y%m%dT%H%M%SZ)
   ```
   Exit 0 = every fixture and drill met expectation. The report has no prompts, responses or secrets.

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
