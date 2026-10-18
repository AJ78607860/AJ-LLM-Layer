"""Hard gates precede all economic ranking."""
from math import sqrt
from .contracts import Model, Authorization
from .tasks import Task, Classification
from .complexity import Complexity


def quality_lower(model: Model) -> float:
    e = model.evidence
    if e is None or e.successes > e.samples:
        return 0.0
    # Wilson 95% lower bound, never a model's self-reported confidence.
    n, p, z = e.samples, e.successes / e.samples, 1.96
    return (p + z*z/(2*n) - z*sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)


def exclusions(model: Model, task: Task, classification: Classification,
               complexity: Complexity, auth: Authorization, *, now: int,
               min_quality: float, min_samples: int, max_age: int,
               max_latency: int, max_failure: float, synthetic: bool) -> tuple[str, ...]:
    reasons = []
    if not model.approved:
        reasons.append('MODEL_NOT_APPROVED')
    if not model.provider_approved:
        reasons.append('PROVIDER_NOT_APPROVED')
    if not model.enabled or model.retired:
        reasons.append('MODEL_DISABLED')
    if model.id not in auth.allowed_models:
        reasons.append('PRIVACY_RESTRICTED')
    if not classification.capabilities <= model.capabilities or not task.modalities <= model.modalities:
        reasons.append('CAPABILITY_MISMATCH')
    if max(task.context_tokens, task.input_tokens + task.output_tokens) > model.context_limit:
        reasons.append('CONTEXT_LIMIT_EXCEEDED')
    if task.output_tokens > model.output_limit:
        reasons.append('OUTPUT_LIMIT_EXCEEDED')
    if auth.region not in model.regions or auth.residency not in model.residencies:
        reasons.append('REGION_RESTRICTED')
    if complexity.band == 'high_stakes' and (not auth.restricted_approved or model.model_class != 'restricted'):
        reasons.append('HIGH_STAKES_RESTRICTED')
    e = model.evidence
    if (e is None or e.successes > e.samples or e.samples < min_samples
            or e.observed_at > now or now - e.observed_at > max_age
            or e.model_version != model.version
            or not set(classification.families) <= e.families
            or (e.source == 'synthetic' and not synthetic)):
        reasons.append('INSUFFICIENT_EVIDENCE')
    if e is not None:
        if quality_lower(model) < min_quality:
            reasons.append('QUALITY_BELOW_THRESHOLD')
        if e.latency_p95_ms > max_latency:
            reasons.append('LATENCY_LIMIT_EXCEEDED')
        if e.failure_rate + e.timeout_rate > max_failure:
            reasons.append('RELIABILITY_BELOW_THRESHOLD')
    return tuple(reasons)
