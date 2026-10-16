"""Pure, zero-token offline model ranking. Not connected to live inference."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


def _metric(value: object) -> bool:
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    cost_per_validated_success_usd: float
    validated_success_lower_bound: float
    latency_p95_ms: float


def rank(candidates: tuple[Candidate, ...], *, min_quality: float,
         max_latency_p95_ms: float) -> tuple[Candidate, ...]:
    """Cheapest qualified model first; fail closed when evidence is missing."""
    if (not _metric(min_quality) or not _metric(max_latency_p95_ms)
            or not 0 <= min_quality <= 1 or max_latency_p95_ms <= 0):
        raise ValueError("invalid routing requirements")
    eligible = []
    for c in candidates:
        values = (c.cost_per_validated_success_usd, c.validated_success_lower_bound, c.latency_p95_ms)
        if not all(_metric(v) for v in values) or min(values) < 0:
            continue
        if c.validated_success_lower_bound < min_quality or c.latency_p95_ms > max_latency_p95_ms:
            continue
        if (c.validated_success_lower_bound > 1
                or not isinstance(c.provider, str) or not c.provider.strip()
                or not isinstance(c.model, str) or not c.model.strip()):
            continue
        eligible.append(c)
    return tuple(sorted(eligible, key=lambda c: (c.cost_per_validated_success_usd, c.latency_p95_ms, c.provider, c.model)))
