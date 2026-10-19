"""Offline, zero-token outcome statistics; never connected to live routing.

Inspired by contextual-bandit research; independently implemented, not copied
from Vowpal Wabbit. Only independently validated rewards should be submitted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, sqrt


def _valid_metric(value: object) -> bool:
    try:
        return type(value) in (int, float) and isfinite(value) and value >= 0
    except OverflowError:
        return False


@dataclass(frozen=True)
class Outcome:
    task_family: str
    model_id: str
    independently_validated: bool
    successful: bool
    total_cost_usd: float
    latency_ms: float


@dataclass
class Bandit:
    """Conservative per-task empirical statistics; no automatic exploration."""
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    successes: dict[tuple[str, str], int] = field(default_factory=dict)
    costs: dict[tuple[str, str], float] = field(default_factory=dict)

    def observe(self, item: Outcome) -> None:
        if item.independently_validated is not True:
            raise ValueError("outcome requires independent validation")
        if (any(not _valid_metric(v)
                for v in (item.total_cost_usd, item.latency_ms))
                or type(item.successful) is not bool):
            raise ValueError("invalid metrics")
        if any(not isinstance(v, str) or not v.strip()
               for v in (item.task_family, item.model_id)):
            raise ValueError("invalid outcome identity")
        key = (item.task_family, item.model_id)
        new_cost = self.costs.get(key, 0) + item.total_cost_usd
        if not isfinite(new_cost):
            raise ValueError("aggregate cost overflow")
        self.counts[key] = self.counts.get(key, 0) + 1
        self.successes[key] = self.successes.get(key, 0) + int(item.successful)
        self.costs[key] = new_cost

    def quality_lower_bound(self, family: str, model_id: str) -> float:
        key = (family, model_id)
        n = self.counts.get(key, 0)
        if n == 0:
            return 0.0
        p = self.successes.get(key, 0) / n
        z = 1.96
        return max(0.0, (p + z*z/(2*n) - z*sqrt((p*(1-p)+z*z/(4*n))/n))/(1+z*z/n))

    def cost_per_success(self, family: str, model_id: str) -> float | None:
        key = (family, model_id)
        successes = self.successes.get(key, 0)
        return self.costs[key] / successes if successes else None
