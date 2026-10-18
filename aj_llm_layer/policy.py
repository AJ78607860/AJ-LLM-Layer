"""Fail-closed policy: the service never trusts a caller's data classification."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingContext:
    task: str
    requested_data_class: str
    tenant_id: str


def classify_data(context: RoutingContext) -> str:
    # Until authenticated tenant/task policy exists, ALL requests are personal.
    # Caller-supplied non_personal labels never downgrade this.
    return "personal"


def eligible_provider(provider: str, data_class: str) -> bool:
    # No live providers authorized in this foundation.
    return False
