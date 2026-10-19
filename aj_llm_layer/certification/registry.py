"""Trusted in-process M1 adapter for synthetic staging certification.

The registry comes from a packaged, reviewed JSON file (never from callers).
Evidence comes only from this Layer's own ledger of independently validated
synthetic outcomes, so it is marked ``source='synthetic'``: it can bootstrap
certification but is not production evidence for customer routing.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources

from ..routing.contracts import Authorization, Evidence, Model, Price, Snapshot
from ..routing.policies import Policy
from .config import CertConfig
from .ledger import Ledger, LedgerUnavailable

TENANT_REF = hashlib.sha256(b"aj-synthetic-certification-tenant.v1").hexdigest()
FEATURE = "synthetic_certification"
EVIDENCE_WINDOW_SECONDS = 7 * 86400

# Trusted platform policy for certification. Tenants cannot relax it.
POLICY = Policy(version="aj-cert-policy.2026-10-19.v1", strategy="cost_efficient", approved=True, enabled=True,
                min_quality=0.80, min_samples=20, max_evidence_age=EVIDENCE_WINDOW_SECONDS,
                max_latency_ms=30000, max_failure=0.20, retries=1)


@dataclass(frozen=True)
class RegistryEntry:
    model_id: str
    provider: str
    api_model: str
    price: dict


def _raw() -> dict:
    override = os.getenv("AJ_LLM_LAYER_CERT_REGISTRY_PATH", "").strip()
    if override:
        with open(override, "rb") as handle:
            return json.loads(handle.read())
    return json.loads(resources.files(__package__).joinpath("staging_registry.json").read_text())


class RegistryUnavailable(Exception):
    """Registry file missing or invalid: refuse (503), never guess."""


def load_registry(cfg: CertConfig, ledger: Ledger, *, now: int, families: set[str] | None = None
                  ) -> tuple[Snapshot, dict[str, RegistryEntry], set[str]]:
    try:
        return _load(cfg, ledger, now=now, families=families)
    except (LedgerUnavailable, RegistryUnavailable):
        raise
    except Exception:
        raise RegistryUnavailable() from None


def _load(cfg, ledger, *, now, families):
    raw = _raw()
    open_circuits = ledger.open_circuits(now=now)
    since = now - EVIDENCE_WINDOW_SECONDS
    models, entries = [], {}
    for item in raw["models"]:
        p = item["price"]
        price = Price(version=raw["price_version"], expires_at=int(raw["expires_at"]), currency="USD",
                      input_per_million=Decimal(p["input"]), output_per_million=Decimal(p["output"]),
                      cached_per_million=Decimal(p["cached"]))
        # Evidence only from outcomes of the requested families (per-family, not pooled).
        stats = ledger.evidence(item["id"], since=since, families=families)
        evidence = None
        if stats is not None and stats["families"]:
            evidence = Evidence(
                version=f"aj-cert-outcomes:{stats['samples']}:{stats['observed_at']}",
                model_version=item["version"], families=frozenset(stats["families"]), source="synthetic",
                samples=stats["samples"], successes=stats["successes"], observed_at=stats["observed_at"],
                latency_p95_ms=stats["latency_p95_ms"], failure_rate=min(1.0, stats["failure_rate"]),
                timeout_rate=min(1.0, stats["timeout_rate"]),
                measured_cost=stats["measured_cost"].quantize(Decimal("0.0000000001")), currency="USD")
        provider_ok = item["provider"] in cfg.approved_providers
        models.append(Model(
            id=item["id"], provider=item["provider"], version=item["version"],
            approved=bool(item["approved"]) and provider_ok, provider_approved=provider_ok,
            # An open circuit disables the model until its cool-down ends (Layer owns provider health).
            enabled=bool(item["enabled"]) and item["id"] not in open_circuits,
            capabilities=frozenset(item["capabilities"]), modalities=frozenset(item["modalities"]),
            context_limit=int(item["context_limit"]), output_limit=int(item["output_limit"]),
            regions=frozenset(item["regions"]), residencies=frozenset(item["residencies"]),
            price=price, evidence=evidence))
        entries[item["id"]] = RegistryEntry(item["id"], item["provider"], item["api_model"], dict(p))
    snapshot = Snapshot(version=raw["version"], expires_at=int(raw["expires_at"]), models=tuple(models))
    return snapshot, entries, open_circuits


def authorize(cfg: CertConfig, snapshot: Snapshot, *, budget_remaining: Decimal, now: int) -> Authorization:
    """Privacy is never relaxed: synthetic certification still uses the personal class (fail closed),
    so only models whose provider is on the approved list may be allowed."""
    allowed = frozenset(m.id for m in snapshot.models if m.provider in cfg.approved_providers and m.approved)
    remaining = min(budget_remaining, cfg.max_run_budget)
    return Authorization(
        tenant_ref=TENANT_REF, version="aj-cert-auth.2026-10-19.v1", expires_at=now + 60, region="GB",
        residency="global", privacy_class="personal", allowed_models=allowed,
        allowed_strategies=frozenset({"cost_efficient"}),
        budget_remaining=remaining.quantize(Decimal("0.0000000001")), currency="USD",
        privacy_available=True, budget_available=remaining > 0,
        feature_enabled=cfg.enabled and cfg.environment_allowed, restricted_approved=False)
