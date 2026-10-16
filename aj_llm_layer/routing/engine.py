"""Pure routing boundary. Recommendations never confer execution authority."""
from hashlib import sha256
import json
from typing import Literal
from pydantic import Field, ValidationError
from .tasks import Contract, Task, classify
from .complexity import assess
from .contracts import Snapshot, Authorization, M1Adapter
from .policies import Policy, tighten
from .eligibility import exclusions, quality_lower
from .economics import estimate, Estimate
from .ranking import Ranked, rank

class Decision(Contract):
    schema_version: Literal['aj.routing.v1'] = 'aj.routing.v1'
    decision_id: str
    correlation_id: str
    tenant_ref: str | None
    timestamp: int
    status: Literal['RECOMMENDATION', 'NO_ELIGIBLE_MODEL']
    execution_authorized: Literal[False] = False
    simulated: bool
    task: dict
    classification: dict
    complexity: dict
    policy: dict
    authorization_version: str | None
    registry_version: str | None
    snapshot_digest: str
    eligible: tuple[Ranked, ...]
    excluded: dict[str, tuple[str, ...]]
    estimates: dict[str, Estimate]
    evidence_versions: dict[str, str]
    model_versions: dict[str, str]
    selected: str | None
    reasons: tuple[str, ...]
    uncertainty: tuple[str, ...] = ('ESTIMATES_NOT_ACTUAL_CHARGES', 'NO_BUDGET_RESERVATION')


def route(task_data: dict, registry: Snapshot | None, auth: Authorization | None,
          platform: Policy, *, now: int, correlation_id: str,
          tenant_policy: Policy | None = None, simulation: bool = False) -> Decision:
    # Revalidate even model_copy/model_construct inputs; caller hints never become authorization.
    registry = Snapshot.model_validate(registry.model_dump()) if registry is not None else None
    auth = Authorization.model_validate(auth.model_dump()) if auth is not None else None
    platform = Policy.model_validate(platform.model_dump())
    if type(now) is not int or now < 0:
        raise ValueError('invalid clock')
    if not correlation_id or len(correlation_id) > 80 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in correlation_id):
        raise ValueError('invalid opaque correlation reference')
    reasons, excluded, rows, estimates = [], {}, [], {}
    task = None
    classification = complexity = None
    policy = platform
    try:
        task = Task.model_validate(task_data)
        if task.cached_tokens > task.input_tokens:
            raise ValueError('invalid cached tokens')
        classification = classify(task)
        complexity = assess(task, classification)
    except (ValidationError, ValueError, TypeError):
        reasons.append('INVALID_TASK_METADATA')
    try:
        policy = tighten(platform, tenant_policy)
    except ValueError:
        reasons.append('POLICY_DOWNGRADE_REJECTED')
    if not platform.approved or not platform.enabled or not policy.approved or not policy.enabled:
        reasons.append('POLICY_DISABLED')
    if registry is None:
        reasons.append('REGISTRY_UNAVAILABLE')
    elif registry.expires_at <= now:
        reasons.append('REGISTRY_EXPIRED')
    if auth is None:
        reasons.append('AUTHORIZATION_UNAVAILABLE')
    else:
        if auth.expires_at <= now:
            reasons.append('AUTHORIZATION_EXPIRED')
        if not auth.privacy_available:
            reasons.append('PRIVACY_UNAVAILABLE')
        if not auth.budget_available:
            reasons.append('BUDGET_UNAVAILABLE')
        if not auth.feature_enabled:
            reasons.append('FEATURE_DISABLED')
        if policy.strategy not in auth.allowed_strategies:
            reasons.append('STRATEGY_NOT_AUTHORIZED')
    if classification is not None and (classification.unknown or complexity.band == 'unknown'):
        reasons.append('UNKNOWN_TASK')
    if not reasons:
        assert task is not None and registry is not None and auth is not None
        high = complexity.band == 'high_stakes' or policy.strategy == 'restricted'
        if high and not auth.restricted_approved:
            reasons.append('HIGH_STAKES_RESTRICTED')
        else:
            for model in sorted(registry.models, key=lambda m: m.id):
                failures = list(exclusions(model, task, classification, complexity, auth, now=now,
                    min_quality=max(policy.min_quality, .95) if high else policy.min_quality,
                    min_samples=max(policy.min_samples, 500) if high else policy.min_samples,
                    max_age=policy.max_evidence_age, max_latency=policy.max_latency_ms,
                    max_failure=min(policy.max_failure, .02) if high else policy.max_failure,
                    synthetic=simulation))
                if high and model.model_class != 'restricted':
                    failures.append('HIGH_STAKES_RESTRICTED')
                # Eligibility before cost optimization; missing prices still fail closed.
                if model.price is None:
                    failures.append('PRICING_UNAVAILABLE')
                if not failures:
                    try:
                        cost = estimate(task, model, policy.retries, auth.currency)
                    except ValueError:
                        failures.append('ECONOMIC_ESTIMATE_UNAVAILABLE')
                    else:
                        if cost.worst_case_cost > auth.budget_remaining:
                            failures.append('BUDGET_EXCEEDED')
                        else:
                            estimates[model.id] = cost
                            rows.append(Ranked(model_id=model.id, quality_lower=quality_lower(model),
                                latency_p95_ms=model.evidence.latency_p95_ms,
                                expected_cost=cost.expected_request_cost,
                                cost_per_success=cost.expected_cost_per_success))
                if failures:
                    excluded[model.id] = tuple(sorted(set(failures)))
    ranked = rank(tuple(rows), policy.strategy)
    canonical = lambda v: json.dumps(v, sort_keys=True, separators=(',', ':'))
    snapshot_digest = sha256(canonical({'registry': registry.model_dump(mode='json') if registry else None,
                                       'authorization': auth.model_dump(mode='json') if auth else None}).encode()).hexdigest()
    data = dict(correlation_id=correlation_id, tenant_ref=auth.tenant_ref if auth else None,
        timestamp=now, status='RECOMMENDATION' if ranked else 'NO_ELIGIBLE_MODEL',
        simulated=simulation, task=task.model_dump(mode='json') if task else {},
        classification=classification.model_dump(mode='json') if classification else {},
        complexity=complexity.model_dump(mode='json') if complexity else {},
        policy=policy.model_dump(mode='json'), authorization_version=auth.version if auth else None,
        registry_version=registry.version if registry else None, snapshot_digest=snapshot_digest,
        eligible=ranked, excluded=excluded, estimates=estimates,
        evidence_versions={m.id:m.evidence.version for m in registry.models if m.evidence} if registry else {},
        model_versions={m.id:m.version for m in registry.models} if registry else {},
        selected=ranked[0].model_id if ranked else None,
        reasons=tuple(reasons) if ranked else tuple(reasons)+('NO_ELIGIBLE_MODEL',))
    provisional = Decision(decision_id='', **data)
    return Decision(decision_id=sha256(canonical(provisional.model_dump(mode='json')).encode()).hexdigest(), **data)


def route_from_m1(task_data: dict, adapter: M1Adapter, *, tenant_ref: str, feature: str,
                  platform: Policy, now: int, correlation_id: str) -> Decision:
    try:
        registry = adapter.registry()
        auth = adapter.authorize(tenant_ref, feature)
        if auth.tenant_ref != tenant_ref:
            raise ValueError('tenant binding mismatch')
        return route(task_data, registry, auth, platform, now=now, correlation_id=correlation_id)
    except Exception:
        # Never log adapter exception text: it may contain sensitive service responses.
        return route(task_data, None, None, platform, now=now, correlation_id=correlation_id)
