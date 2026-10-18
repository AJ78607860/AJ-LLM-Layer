"""Pure, zero-token offline model ranking. Not connected to live inference."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


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
    if not 0 <= min_quality <= 1 or max_latency_p95_ms <= 0:
        raise ValueError("invalid routing requirements")
    eligible = []
    for c in candidates:
        values = (c.cost_per_validated_success_usd, c.validated_success_lower_bound, c.latency_p95_ms)
        if not all(isfinite(v) for v in values) or min(values) < 0:
            continue
        if c.validated_success_lower_bound < min_quality or c.latency_p95_ms > max_latency_p95_ms:
            continue
        if c.validated_success_lower_bound > 1 or not c.provider or not c.model:
            continue
        eligible.append(c)
    return tuple(sorted(eligible, key=lambda c: (c.cost_per_validated_success_usd, c.latency_p95_ms, c.provider, c.model)))
