"""Strict M1 integration boundary; these are proposed contracts, not M1 certification.

Only trusted M1 adapters may construct snapshots and authorization. Task metadata
cannot supply them. Synthetic fixtures explicitly identify their provenance.
"""
from decimal import Decimal
from typing import Literal, Protocol
from pydantic import Field, field_validator
from .tasks import Contract

class Price(Contract):
    version: str = Field(min_length=1, max_length=80)
    expires_at: int = Field(ge=0)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    input_per_million: Decimal = Field(ge=0, max_digits=20, decimal_places=10)
    output_per_million: Decimal = Field(ge=0, max_digits=20, decimal_places=10)
    cached_per_million: Decimal = Field(ge=0, max_digits=20, decimal_places=10)

class Evidence(Contract):
    version: str = Field(min_length=1, max_length=80)
    model_version: str = Field(min_length=1, max_length=80)
    families: frozenset[str] = Field(max_length=16)
    source: Literal['independent', 'synthetic']
    samples: int = Field(ge=1, le=10_000_000)
    successes: int = Field(ge=0)
    observed_at: int = Field(ge=0)
    latency_p95_ms: int = Field(gt=0, le=3_600_000)
    failure_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    timeout_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    measured_cost: Decimal = Field(ge=0, max_digits=20, decimal_places=10)
    currency: str = Field(pattern=r'^[A-Z]{3}$')

class Model(Contract):
    id: str = Field(pattern=r'^[a-zA-Z0-9._/-]{1,100}$')
    provider: str = Field(pattern=r'^[a-zA-Z0-9._-]{1,60}$')
    version: str = Field(min_length=1, max_length=80)
    approved: bool
    provider_approved: bool
    enabled: bool
    retired: bool = False
    model_class: Literal['standard', 'restricted'] = 'standard'
    capabilities: frozenset[str]
    modalities: frozenset[str]
    context_limit: int = Field(gt=0, le=10_000_000)
    output_limit: int = Field(gt=0, le=1_000_000)
    regions: frozenset[str]
    residencies: frozenset[str]
    price: Price | None
    evidence: Evidence | None

class Snapshot(Contract):
    version: str = Field(min_length=1, max_length=80)
    expires_at: int = Field(ge=0)
    models: tuple[Model, ...] = Field(max_length=2000)
    @field_validator('models')
    @classmethod
    def unique(cls, models):
        if len({m.id for m in models}) != len(models):
            raise ValueError('duplicate model identity')
        return models

class Authorization(Contract):
    """M1 policy result, trusted separately from caller task hints."""
    tenant_ref: str = Field(pattern=r'^[a-f0-9]{64}$')
    version: str = Field(min_length=1, max_length=80)
    expires_at: int = Field(ge=0)
    region: str = Field(min_length=1, max_length=40)
    residency: str = Field(min_length=1, max_length=40)
    privacy_class: Literal['personal', 'restricted', 'non_personal']
    allowed_models: frozenset[str]
    allowed_strategies: frozenset[str]
    budget_remaining: Decimal = Field(ge=0, max_digits=20, decimal_places=10)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    privacy_available: bool
    budget_available: bool
    feature_enabled: bool
    restricted_approved: bool = False

class M1Adapter(Protocol):
    def registry(self) -> Snapshot: ...
    def authorize(self, tenant_ref: str, feature: str) -> Authorization: ...
