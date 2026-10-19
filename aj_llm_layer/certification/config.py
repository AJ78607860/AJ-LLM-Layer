"""Certification configuration, read per request. Every default denies."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Environments where synthetic certification may run. Anything else (including
# "production", "prod" or unset) makes every certification route answer 404.
ALLOWED_ENVIRONMENTS = frozenset({"staging", "development", "local", "test"})
HARD_MAX_DAILY_USD = Decimal("5")     # code ceiling; config cannot exceed it
HARD_MAX_RUN_USD = Decimal("0.50")
HARD_MAX_OUTPUT_TOKENS = 4096
HARD_MAX_CONCURRENT = 8


def _decimal(name: str, default: str, ceiling: Decimal) -> Decimal:
    try:
        value = Decimal((os.getenv(name) or default).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if not value.is_finite() or value < 0:
        return Decimal("0")
    return min(value, ceiling)


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return lo
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class CertConfig:
    environment: str
    enabled: bool
    daily_budget: Decimal
    max_run_budget: Decimal
    max_output_tokens: int
    max_concurrent: int
    approved_providers: frozenset[str]
    ledger_path: str
    grant_ttl_seconds: int
    breaker_threshold: int
    breaker_cooldown_seconds: int
    probe_daily_limit: int

    @property
    def environment_allowed(self) -> bool:
        return self.environment in ALLOWED_ENVIRONMENTS


def load() -> CertConfig:
    env = (os.getenv("AJ_ENVIRONMENT") or "").strip().lower()
    providers = frozenset(p.strip().lower() for p in
                          (os.getenv("AJ_LLM_LAYER_CERT_APPROVED_PROVIDERS") or "openai").split(",") if p.strip())
    return CertConfig(
        environment=env,
        enabled=(os.getenv("AJ_LLM_LAYER_CERTIFICATION_ENABLED") or "").strip().lower() == "true",
        # Default 0: no budget configured means no billable grant can ever be issued.
        daily_budget=_decimal("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD", "0", HARD_MAX_DAILY_USD),
        max_run_budget=_decimal("AJ_LLM_LAYER_CERT_MAX_RUN_USD", "0.02", HARD_MAX_RUN_USD),
        max_output_tokens=_int("AJ_LLM_LAYER_CERT_MAX_OUTPUT_TOKENS", 512, 1, HARD_MAX_OUTPUT_TOKENS),
        max_concurrent=_int("AJ_LLM_LAYER_CERT_MAX_CONCURRENT", 2, 1, HARD_MAX_CONCURRENT),
        approved_providers=providers,
        ledger_path=(os.getenv("AJ_LLM_LAYER_CERT_LEDGER_PATH") or "").strip(),
        grant_ttl_seconds=_int("AJ_LLM_LAYER_CERT_GRANT_TTL_SECONDS", 120, 10, 600),
        breaker_threshold=_int("AJ_LLM_LAYER_CERT_BREAKER_THRESHOLD", 3, 1, 20),
        breaker_cooldown_seconds=_int("AJ_LLM_LAYER_CERT_BREAKER_COOLDOWN_SECONDS", 300, 5, 86400),
        probe_daily_limit=_int("AJ_LLM_LAYER_CERT_PROBE_DAILY_LIMIT", 40, 0, 1000),
    )
