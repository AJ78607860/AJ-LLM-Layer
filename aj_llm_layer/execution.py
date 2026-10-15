"""Provider-neutral execution contract; no live credentials or default egress.

LiteLLM-compatible concepts, independently implemented. Execution requires
a separately authorized adapter and security review.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Request:
    model: str
    task_family: str
    prompt: str
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class Result:
    model: str
    text: str
    latency_ms: float
    billed_cost_usd: float


class Adapter(Protocol):
    def complete(self, request: Request) -> Result: ...


class DisabledAdapter:
    def complete(self, request: Request) -> Result:
        raise PermissionError("provider egress disabled pending security approval")
