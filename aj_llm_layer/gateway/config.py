"""Gateway configuration, read from environment variables only.

Secrets are never logged or returned. Anything missing makes that provider
unavailable (fail closed) rather than guessing.

Environment variables
---------------------
OPENAI_API_KEY / ANTHROPIC_API_KEY      provider keys (or AJ_LLM_OPENAI_API_KEY / AJ_LLM_ANTHROPIC_API_KEY)
AJ_LLM_PROVIDER_ORDER                   preference order, default "openai,anthropic"
AJ_LLM_<PROVIDER>_MODEL_<TIER>          model per tier (fast|standard|smart)
AJ_LLM_PERSONAL_DATA_PROVIDERS          providers approved for personal data, default "openai"
                                        (ADR-025). Add "anthropic" only after owner approval + DPA.
AJ_LLM_PRICES                           optional JSON {"model-id": [usd_in_per_million, usd_out_per_million]}
AJ_LLM_SPEND_LIMITS                     optional JSON {"openai": 20, "anthropic": 50} USD per month;
                                        provider is skipped once reached (needs AJ_LLM_PRICES)
AJ_LLM_STATE_REDIS_URL                  optional; shares credit/health state across replicas
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

TIERS = ("fast", "standard", "smart")
KNOWN_PROVIDERS = ("openai", "anthropic")

# Anthropic's current public model IDs. OpenAI has no default on purpose: AJ must
# state which model it runs today (roadmap decision D4) instead of us guessing.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "openai": {},
    "anthropic": {"fast": "claude-haiku-5-5", "standard": "claude-sonnet-5-5",
                  "smart": "claude-opus-5-5"},
}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str = field(repr=False)
    models: dict[str, str]
    personal_data_approved: bool
    base_url: str | None = None


@dataclass(frozen=True)
class GatewayConfig:
    providers: tuple[ProviderConfig, ...]
    prices: dict[str, tuple[float, float]]
    spend_limits: dict[str, float] = field(default_factory=dict)

    def status(self) -> dict:
        """Content- and secret-free readiness summary."""
        return {p.name: {"key_configured": bool(p.api_key),
                         "models": dict(p.models),
                         "personal_data_approved": p.personal_data_approved}
                for p in self.providers}


def _csv(value: str | None, default: str) -> list[str]:
    raw = value if value is not None and value.strip() else default
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def load_config(env: dict[str, str] | None = None) -> GatewayConfig:
    env = dict(os.environ if env is None else env)
    order = [p for p in _csv(env.get("AJ_LLM_PROVIDER_ORDER"), "openai,anthropic")
             if p in KNOWN_PROVIDERS]
    order = list(dict.fromkeys(order))  # de-duplicate, keep order
    personal = set(_csv(env.get("AJ_LLM_PERSONAL_DATA_PROVIDERS"), "openai"))
    providers = []
    for name in order:
        upper = name.upper()
        key = (env.get(f"AJ_LLM_{upper}_API_KEY") or env.get(f"{upper}_API_KEY") or "").strip()
        models = dict(DEFAULT_MODELS.get(name, {}))
        for tier in TIERS:
            override = (env.get(f"AJ_LLM_{upper}_MODEL_{tier.upper()}") or "").strip()
            if override:
                models[tier] = override
        providers.append(ProviderConfig(
            name=name, api_key=key, models=models,
            personal_data_approved=name in personal,
            base_url=(env.get(f"AJ_LLM_{upper}_BASE_URL") or "").strip() or None))
    return GatewayConfig(providers=tuple(providers), prices=_prices(env.get("AJ_LLM_PRICES")),
                         spend_limits=_limits(env.get("AJ_LLM_SPEND_LIMITS")))


def _limits(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k).lower(): float(v) for k, v in data.items() if float(v) >= 0}
    except (ValueError, TypeError, AttributeError):
        return {}


def _prices(raw: str | None) -> dict[str, tuple[float, float]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): (float(v[0]), float(v[1])) for k, v in data.items()}
    except (ValueError, TypeError, IndexError, AttributeError):
        return {}  # bad price table → costs reported as unknown, never wrong
