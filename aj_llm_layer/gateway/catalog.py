"""The model "supermarket": every model the Layer can buy, with tier, price and limits.

Ships with `catalog.json` (prices checked against each provider's pricing page on the
date recorded per model). Override with `AJ_LLM_CATALOG_PATH`, switch individual
models off with `AJ_LLM_DISABLED_MODELS=model-a,model-b`.

Tiers describe the class of model, not a provider: `fast` (cheap, quick),
`standard`, `smart` (strongest). A request asks for a tier; every provider's
models in that tier compete on evidence and price.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources

TIER_RANK = {"fast": 0, "standard": 1, "smart": 2}


@dataclass(frozen=True)
class CatalogModel:
    id: str
    provider: str
    tier: str
    input: float           # USD per million input tokens
    cached: float
    output: float
    context_limit: int
    output_limit: int
    long_above: int | None = None
    long_input: float | None = None
    long_cached: float | None = None
    long_output: float | None = None
    price_verified: bool = False
    price_checked: str = ""

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        inp, cac, out = self.input, self.cached, self.output
        if self.long_above is not None and input_tokens > self.long_above:
            inp, cac, out = self.long_input, self.long_cached, self.long_output
        fresh = max(0, input_tokens - cached_tokens)
        return round((fresh * inp + cached_tokens * cac + output_tokens * out) / 1_000_000, 8)


@dataclass(frozen=True)
class Catalog:
    version: str
    models: tuple[CatalogModel, ...]

    def get(self, model_id: str) -> CatalogModel | None:
        return next((m for m in self.models if m.id == model_id), None)

    def default_for(self, provider: str, tier: str) -> str | None:
        """Cheapest catalogued model for a provider in a tier (used by `ordered` mode)."""
        options = [m for m in self.models if m.provider == provider and m.tier == tier]
        return min(options, key=lambda m: (m.input + m.output, m.id)).id if options else None


def _parse(data: dict, disabled: set[str]) -> Catalog:
    models = []
    for raw in data.get("models", []):
        if raw["id"] in disabled:
            continue
        if raw["tier"] not in TIER_RANK:
            raise ValueError(f"unknown tier for {raw['id']}")
        long = raw.get("long_context") or {}
        models.append(CatalogModel(
            id=raw["id"], provider=raw["provider"], tier=raw["tier"],
            input=float(raw["input"]), cached=float(raw.get("cached", raw["input"])),
            output=float(raw["output"]), context_limit=int(raw["context_limit"]),
            output_limit=int(raw["output_limit"]),
            long_above=int(long["above_input_tokens"]) if long else None,
            long_input=float(long["input"]) if long else None,
            long_cached=float(long.get("cached", long.get("input"))) if long else None,
            long_output=float(long["output"]) if long else None,
            price_verified=bool(raw.get("price_verified", False)),
            price_checked=str(raw.get("price_checked", ""))))
    if len({m.id for m in models}) != len(models):
        raise ValueError("duplicate model id in catalog")
    return Catalog(version=str(data.get("version", "unversioned")), models=tuple(models))


def load_catalog(env: dict[str, str] | None = None) -> Catalog:
    env = dict(os.environ if env is None else env)
    disabled = {m.strip() for m in (env.get("AJ_LLM_DISABLED_MODELS") or "").split(",") if m.strip()}
    path = (env.get("AJ_LLM_CATALOG_PATH") or "").strip()
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = json.loads(resources.files("aj_llm_layer.gateway").joinpath("catalog.json").read_text())
    return _parse(data, disabled)
