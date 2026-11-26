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


class GatewayAdapter:
    """Executes a routing decision through the gateway (aj_llm_layer.gateway).

    The routing engine's chosen provider and model are pinned, so routed calls go
    exactly where routing sent them; with `allow_fallback` the gateway's normal
    provider order (and tier models) is used instead.
    """

    def __init__(self, gateway, *, provider: str, data_class: str = "personal",
                 allow_fallback: bool = False):
        self._gateway = gateway
        self._provider = provider
        self._data_class = data_class
        self._allow_fallback = allow_fallback

    def complete(self, request: Request) -> Result:
        import re
        from .gateway import CompletionRequest, Message
        purpose = request.task_family if re.fullmatch(r"[a-z0-9_]{1,64}", request.task_family) else "general"
        pinned = not self._allow_fallback
        result = self._gateway.complete(CompletionRequest(
            messages=(Message(role="user", content=request.prompt),),
            purpose=purpose, data_class=self._data_class, timeout_seconds=request.timeout_seconds,
            providers=(self._provider,) if pinned else None,
            model=request.model if pinned else None), app="routing")
        return Result(model=result.model, text=result.text, latency_ms=float(result.latency_ms),
                      billed_cost_usd=result.cost_usd or 0.0)
