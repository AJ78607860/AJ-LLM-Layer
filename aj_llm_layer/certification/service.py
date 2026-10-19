"""Certification orchestration: decide (route + reserve + grant) and outcome (settle + learn)."""
from __future__ import annotations

import json
import secrets
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..routing.engine import route
from ..routing.eligibility import quality_lower
from ..routing.tasks import Task
from .config import CertConfig
from .ledger import BudgetRefused, GrantRejected, Ledger
from .registry import POLICY, authorize, load_registry

TASK_CLASSES = ("simple_extraction", "summarisation", "general_reasoning", "multimodal", "tool_orchestration",
                "complex_planning", "financial_interpretation", "safety_critical")
TRANSPORT_ERRORS = frozenset({"timeout", "server_error", "rate_limited", "connection"})
# While a model is still below the evidence minimum, a few early failures must not lock it out for
# the whole evidence window; consecutive transport failures are stopped by the circuit breaker instead.
PROBE_REASONS = frozenset({"INSUFFICIENT_EVIDENCE", "QUALITY_BELOW_THRESHOLD", "RELIABILITY_BELOW_THRESHOLD"})
MAX_ATTEMPT_TIMEOUT_SECONDS = 30
_Q = Decimal("0.00000001")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CortexMeta(Strict):
    task_class: Literal[TASK_CLASSES]  # type: ignore[valid-type]
    tier: Literal["fast", "standard", "strong"]
    data_class: Literal["personal"]


class DecideRequest(Strict):
    schema_: Literal["aj.cert.decide.v1"] = Field(alias="schema")
    fixture_id: str = Field(pattern=r"^cert_[a-z0-9_]{1,48}$")
    cortex: CortexMeta
    task: dict


class Attempt(Strict):
    input_tokens: int = Field(ge=0, le=2_000_000)
    cached_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(ge=0, le=200_000)
    latency_ms: int = Field(ge=0, le=600_000)
    error: Literal["timeout", "server_error", "rate_limited", "connection", "client_error", "empty_output"] | None
    usage_known: bool


class Validation(Strict):
    passed: bool
    validator: str = Field(pattern=r"^[a-z0-9_.]{1,64}$")


class OutcomeRequest(Strict):
    schema_: Literal["aj.cert.outcome.v1"] = Field(alias="schema")
    grant_id: str = Field(pattern=r"^grt_[a-f0-9]{32}$")
    status: Literal["succeeded", "failed", "refused_by_policy", "not_executed"]
    attempts: tuple[Attempt, ...] = Field(max_length=6)
    validation: Validation | None = None


def _ceil(value: Decimal) -> Decimal:
    return value.quantize(_Q, rounding=ROUND_CEILING)


def _cost(price: dict, *, input_tokens: int, cached: int, output: int) -> Decimal:
    cached = min(cached, input_tokens)
    return ((input_tokens - cached) * Decimal(price["input"]) + cached * Decimal(price["cached"])
            + output * Decimal(price["output"])) / Decimal(1_000_000)


def decide(req: DecideRequest, *, cfg: CertConfig, ledger: Ledger, correlation_id: str, now: int) -> dict:
    ledger.sweep(now=now)
    budget = ledger.budget(daily_cap=cfg.daily_budget, now=now)
    snapshot, entries, open_circuits = load_registry(cfg, ledger, now=now)
    auth = authorize(cfg, snapshot, budget_remaining=Decimal(budget["remaining"]), now=now)
    try:
        # JSON-mode validation (lists -> tuples/sets) before the strict python-mode routing boundary.
        task_data = Task.model_validate_json(json.dumps(req.task)).model_dump()
    except (ValidationError, ValueError, TypeError):
        task_data = {"invalid": True}
    decision = route(task_data, snapshot, auth, POLICY, now=now, correlation_id=correlation_id, simulation=True)
    grant = None
    refusal = None
    kind = None
    if decision.status == "RECOMMENDATION":
        kind, model_id = "route", decision.selected
        amount = decision.estimates[model_id].worst_case_cost
        attempts = POLICY.retries + 1
    else:
        # Evidence bootstrap: only when nothing but missing evidence blocks an otherwise allowed model.
        global_reasons = set(decision.reasons) - {"NO_ELIGIBLE_MODEL"}
        candidates = []
        over_budget = False
        if not global_reasons and cfg.probe_daily_limit > 0:
            models = {m.id: m for m in snapshot.models}
            for model_id, reasons in decision.excluded.items():
                model = models[model_id]
                samples = model.evidence.samples if model.evidence else 0
                if (set(reasons) <= PROBE_REASONS and samples < POLICY.min_samples
                        and ledger.probes_today(model_id, now=now) < cfg.probe_daily_limit):
                    t = decision.task
                    unit = _ceil(_cost(entries[model_id].price, input_tokens=t["input_tokens"],
                                       cached=t["cached_tokens"], output=t["output_tokens"]))
                    if unit <= auth.budget_remaining:
                        candidates.append((unit, model_id))
                    else:
                        over_budget = True
        if candidates:
            amount, model_id = min(candidates)
            kind, attempts = "probe", 1
        else:
            refusal = next((r for r in decision.reasons if r != "NO_ELIGIBLE_MODEL"),
                           "BUDGET_EXCEEDED" if over_budget else "NO_ELIGIBLE_MODEL")
    budget_after = budget
    if kind is not None:
        entry = entries[model_id]
        model = next(m for m in snapshot.models if m.id == model_id)
        grant = {
            "grant_id": "grt_" + secrets.token_hex(16), "reservation_id": "rsv_" + secrets.token_hex(16),
            "decision_id": decision.decision_id, "correlation_id": correlation_id, "kind": kind,
            "model_id": model_id, "provider": entry.provider, "api_model": entry.api_model,
            "families": list(decision.task.get("families", [])),
            "max_input_tokens": decision.task["input_tokens"],
            "max_output_tokens": min(decision.task["output_tokens"], cfg.max_output_tokens, model.output_limit),
            "max_attempts": attempts,
            "timeout_seconds": min(MAX_ATTEMPT_TIMEOUT_SECONDS, POLICY.max_latency_ms // 1000),
            "price": entry.price,
        }
        # The grant must outlive every permitted attempt plus reporting time, or a slow provider
        # would be charged the full hold as "abandoned".
        grant["expires_at"] = now + max(cfg.grant_ttl_seconds,
                                        grant["max_attempts"] * grant["timeout_seconds"] + 30)
        try:
            ledger.reserve_and_grant(grant, amount=amount, daily_cap=cfg.daily_budget, max_run=cfg.max_run_budget,
                                     max_concurrent=cfg.max_concurrent, now=now)
            grant["reserved"] = str(amount)
            budget_after = ledger.budget(daily_cap=cfg.daily_budget, now=now)
        except BudgetRefused as refused:
            refusal, grant = refused.code, None
    record = {
        "mode": "certification", "fixture_id": req.fixture_id, "cortex": req.cortex.model_dump(),
        "decision": decision.model_dump(mode="json"),
        "grant": dict(grant) if grant else None,
        "refusal": refusal, "circuit_open": sorted(open_circuits), "budget_before": budget,
        "budget_after": budget_after, "production_routing_changed": False,
    }
    ledger.record_decision(decision.decision_id, correlation_id, record, now=now)
    return record


def outcome(req: OutcomeRequest, *, cfg: CertConfig, ledger: Ledger, now: int) -> dict:
    ledger.sweep(now=now)
    grant = ledger.grant(req.grant_id)
    if grant is None:
        raise GrantRejected("UNKNOWN_GRANT")
    if grant["status"] == "expired":
        raise GrantRejected("GRANT_EXPIRED")
    if grant["status"] != "issued":
        raise GrantRejected("GRANT_ALREADY_CLOSED")
    executed = req.status in ("succeeded", "failed")
    if not executed and req.attempts:
        raise GrantRejected("ATTEMPTS_WITHOUT_EXECUTION")
    if executed and (not req.attempts or req.validation is None):
        raise GrantRejected("INCOMPLETE_OUTCOME")
    last = req.attempts[-1] if req.attempts else None
    if req.status == "succeeded" and (not req.validation.passed or last.error is not None):
        raise GrantRejected("INCONSISTENT_OUTCOME")
    flags: set[str] = set()
    if len(req.attempts) > grant["max_attempts"]:
        flags.add("ATTEMPT_BOUND_EXCEEDED")
    charged = Decimal("0")
    for attempt in req.attempts:
        if attempt.output_tokens > grant["max_output_tokens"]:
            flags.add("OUTPUT_BOUND_EXCEEDED")
        if attempt.input_tokens > grant["max_input_tokens"]:
            flags.add("INPUT_BOUND_EXCEEDED")
        if attempt.usage_known:
            charged += _cost(grant["price"], input_tokens=attempt.input_tokens, cached=attempt.cached_tokens,
                             output=attempt.output_tokens)
        else:
            # Unknown usage (e.g. timeout): charge the attempt's full bound, never zero.
            flags.add("USAGE_UNKNOWN_CHARGED_AT_BOUND")
            charged += _cost(grant["price"], input_tokens=grant["max_input_tokens"], cached=0,
                             output=grant["max_output_tokens"])
    charged = _ceil(charged)
    transport = [a for a in req.attempts if a.error in TRANSPORT_ERRORS]
    row = {
        "model_id": grant["model_id"], "families": grant["families"], "status": req.status,
        "attempts": len(req.attempts), "input_tokens": sum(a.input_tokens for a in req.attempts),
        "output_tokens": sum(a.output_tokens for a in req.attempts),
        "latency_ms": sum(a.latency_ms for a in req.attempts),
        "validated": req.status == "succeeded", "transport_failures": len(transport),
        "timeouts": sum(1 for a in transport if a.error == "timeout"),
        # Contract violations never become routing evidence.
        "counted": executed and not (flags - {"USAGE_UNKNOWN_CHARGED_AT_BOUND"}), "flags": sorted(flags),
    }
    settlement = ledger.settle(req.grant_id, charged=charged, outcome=row, now=now)
    if Decimal(settlement["overspend"]) > 0:
        flags.add("RESERVATION_EXCEEDED")
    health = None
    if executed:
        health = ledger.record_health(
            grant["model_id"], transport_failure=req.status == "failed" and last.error in TRANSPORT_ERRORS,
            threshold=cfg.breaker_threshold, cooldown=cfg.breaker_cooldown_seconds, now=now)
    return {"mode": "certification", "grant_id": req.grant_id, "decision_id": grant["decision_id"],
            "kind": grant["kind"], "model_id": grant["model_id"], "status": req.status,
            "counted_as_evidence": row["counted"], "flags": sorted(flags), "settlement": settlement,
            "health": health, "budget": ledger.budget(daily_cap=cfg.daily_budget, now=now)}


def status(*, cfg: CertConfig, ledger: Ledger, now: int) -> dict:
    ledger.sweep(now=now)
    snapshot, _, open_circuits = load_registry(cfg, ledger, now=now)
    evidence = {}
    for m in snapshot.models:
        if m.evidence is not None:
            evidence[m.id] = {"samples": m.evidence.samples, "successes": m.evidence.successes,
                              "quality_lower_95": round(quality_lower(m), 4),
                              "latency_p95_ms": m.evidence.latency_p95_ms, "families": sorted(m.evidence.families),
                              "source": m.evidence.source}
    return {"mode": "certification", "environment": cfg.environment, "enabled": cfg.enabled,
            "approved_providers": sorted(cfg.approved_providers), "policy_version": POLICY.version,
            "registry_version": snapshot.version, "max_run_budget": str(cfg.max_run_budget),
            "budget": ledger.budget(daily_cap=cfg.daily_budget, now=now), "health": ledger.health(now=now),
            "circuit_open": sorted(open_circuits), "evidence": evidence, "production_routing_changed": False}
