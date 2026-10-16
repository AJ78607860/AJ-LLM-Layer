"""Platform policy is trusted configuration; tenants may only tighten constraints."""
from typing import Literal
from pydantic import Field
from .tasks import Contract

Strategy = Literal['quality_first', 'balanced', 'cost_efficient', 'latency_sensitive', 'restricted']

class Policy(Contract):
    version: str = Field(min_length=1, max_length=80)
    strategy: Strategy = 'balanced'
    approved: bool = False
    enabled: bool = False
    min_quality: float = Field(default=.8, ge=0, le=1, allow_inf_nan=False)
    min_samples: int = Field(default=100, ge=1, le=10_000_000)
    max_evidence_age: int = Field(default=86400*30, ge=1, le=86400*365)
    max_latency_ms: int = Field(default=10000, gt=0, le=3_600_000)
    max_failure: float = Field(default=.1, ge=0, le=1, allow_inf_nan=False)
    retries: int = Field(default=1, ge=0, le=5)


def tighten(platform: Policy, tenant: Policy | None) -> Policy:
    if tenant is None:
        return platform
    if (not tenant.approved or not tenant.enabled or tenant.min_quality < platform.min_quality
            or tenant.min_samples < platform.min_samples
            or tenant.max_evidence_age > platform.max_evidence_age
            or tenant.max_latency_ms > platform.max_latency_ms
            or tenant.max_failure > platform.max_failure
            or tenant.retries > platform.retries
            or (platform.strategy == 'restricted' and tenant.strategy != 'restricted')):
        raise ValueError('POLICY_DOWNGRADE_REJECTED')
    return tenant

class PolicyCatalog(Contract):
    policies: tuple[Policy, ...]
    active_version: str

    def active(self) -> Policy:
        matches = [p for p in self.policies if p.version == self.active_version]
        if len(matches) != 1 or not matches[0].approved or not matches[0].enabled:
            raise ValueError('INVALID_POLICY_SNAPSHOT')
        return matches[0]
