from decimal import Decimal
from .tasks import Contract

class Ranked(Contract):
    model_id: str
    quality_lower: float
    latency_p95_ms: int
    expected_cost: Decimal
    cost_per_success: Decimal
    pareto: bool = False


def rank(rows: tuple[Ranked, ...], strategy: str) -> tuple[Ranked, ...]:
    result = []
    for row in rows:
        dominated = any(
            other.quality_lower >= row.quality_lower and other.expected_cost <= row.expected_cost
            and other.latency_p95_ms <= row.latency_p95_ms
            and (other.quality_lower > row.quality_lower or other.expected_cost < row.expected_cost
                 or other.latency_p95_ms < row.latency_p95_ms) for other in rows)
        result.append(row.model_copy(update={'pareto': not dominated}))
    def key(r):
        if strategy in {'quality_first', 'restricted'}:
            return (-r.quality_lower, r.cost_per_success, r.latency_p95_ms, r.model_id)
        if strategy == 'latency_sensitive':
            return (r.latency_p95_ms, r.cost_per_success, -r.quality_lower, r.model_id)
        if strategy == 'balanced':
            return (not r.pareto, r.expected_cost, r.latency_p95_ms, -r.quality_lower, r.model_id)
        return (r.cost_per_success, r.latency_p95_ms, -r.quality_lower, r.model_id)
    return tuple(sorted(result, key=key))
