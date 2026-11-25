"""Provider-agnostic model selection: the right model for the task, cheapest that is proven correct.

For each request the selector builds a shortlist from the catalogue and orders it:

1. **Hard gates.** Drop models whose provider has no key, may not see this data
   (personal data → approved providers only), is benched for credit/auth problems
   or over its spend limit, or whose context/output limits are too small.
2. **Proven models first.** Models in the requested tier with enough graded
   evidence for this task family go through the routing engine
   (`aj_llm_layer.routing.engine`): it removes any model below the quality bar or
   reliability bar and ranks the rest by the chosen strategy, by default
   `cost_efficient` = lowest expected cost per *correct* answer.
3. **Untested next.** Models without enough evidence yet, cheapest first for this
   request's token counts. A small share of traffic (`AJ_LLM_EXPLORE_RATE`) tries an
   untested model first so evidence keeps growing.
4. **Proven-poor last.** Models with enough evidence that they fall below the bar
   are only used when nothing better is available.
5. **Stronger tiers as backup.** If every model in the requested tier fails, the
   next tier up is tried (never a weaker one).

No provider is preferred by name. Order comes from evidence and price only.
"""
from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ..routing.contracts import Authorization, Model, Price, Snapshot
from ..routing.engine import route
from ..routing.policies import Policy
from ..routing.tasks import FAMILIES
from .catalog import TIER_RANK, Catalog, CatalogModel
from .config import ProviderConfig
from .evidence import EvidenceLedger
from .health import ProviderHealth
from .types import Attempt, CompletionRequest

ALL_CAPABILITIES = frozenset().union(*FAMILIES.values())
STRATEGIES = ("cost_efficient", "quality_first", "balanced", "latency_sensitive")
DEMOTE = {"QUALITY_BELOW_THRESHOLD", "RELIABILITY_BELOW_THRESHOLD", "LATENCY_LIMIT_EXCEEDED"}


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    selection: str  # proven | untested | explore | proven_poor | stronger_tier | pinned | ordered


@dataclass(frozen=True)
class SelectorSettings:
    strategy: str = "cost_efficient"
    min_quality: float = 0.8
    min_samples: int = 30
    max_latency_ms: int = 120_000
    max_failure: float = 0.2
    max_evidence_age_days: int = 60
    explore_rate: float = 0.05

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SelectorSettings":
        env = dict(os.environ if env is None else env)

        def num(name, cast, default, lo, hi):
            try:
                value = cast(env.get(name, default))
            except (TypeError, ValueError):
                return default
            return default if not lo <= value <= hi else value
        strategy = (env.get("AJ_LLM_STRATEGY") or "cost_efficient").strip()
        return cls(strategy=strategy if strategy in STRATEGIES else "cost_efficient",
                   min_quality=num("AJ_LLM_MIN_QUALITY", float, 0.8, 0.0, 1.0),
                   min_samples=num("AJ_LLM_MIN_SAMPLES", int, 30, 1, 1_000_000),
                   max_latency_ms=num("AJ_LLM_MAX_LATENCY_MS", int, 120_000, 1, 3_600_000),
                   max_failure=num("AJ_LLM_MAX_FAILURE", float, 0.2, 0.0, 1.0),
                   max_evidence_age_days=num("AJ_LLM_MAX_EVIDENCE_AGE_DAYS", int, 60, 1, 365),
                   explore_rate=num("AJ_LLM_EXPLORE_RATE", float, 0.05, 0.0, 1.0))


def estimated_input_tokens(request: CompletionRequest) -> int:
    chars = sum(len(m.content) for m in request.messages) + len(request.system or "")
    return chars // 4 + 8 * len(request.messages)


class Selector:
    def __init__(self, catalog: Catalog, ledger: EvidenceLedger, health: ProviderHealth,
                 providers: dict[str, ProviderConfig], available: set[str],
                 settings: SelectorSettings, *, clock: Callable[[], float],
                 rng: random.Random | None = None):
        self.catalog = catalog
        self.ledger = ledger
        self.health = health
        self.providers = providers
        self.available = available  # providers with a client (i.e. an API key)
        self.settings = settings
        self._clock = clock
        self._rng = rng or random.Random()

    # -- gates -------------------------------------------------------------------
    def _gate(self, m: CatalogModel, request: CompletionRequest, input_tokens: int) -> str | None:
        cfg = self.providers.get(m.provider)
        if cfg is None:
            return "provider_not_configured"
        if request.data_class == "personal" and not cfg.personal_data_approved:
            return "personal_data_not_approved"
        if m.provider not in self.available:
            return "no_api_key"
        blocked = self.health.blocked(m.provider)
        if blocked:
            return blocked
        if input_tokens + request.max_tokens > m.context_limit:
            return "context_limit"
        if request.max_tokens > m.output_limit:
            return "output_limit"
        return None

    # -- main ----------------------------------------------------------------------
    def shortlist(self, request: CompletionRequest) -> tuple[list[Candidate], list[Attempt], dict]:
        from .understand import resolve
        request, _ = resolve(request)
        input_tokens = estimated_input_tokens(request)
        skipped: list[Attempt] = []
        wanted = {p.lower() for p in request.providers} if request.providers else None
        pool: list[CatalogModel] = []
        for m in self.catalog.models:
            if wanted is not None and m.provider not in wanted:
                continue
            if TIER_RANK[m.tier] < TIER_RANK[request.tier]:
                continue
            reason = self._gate(m, request, input_tokens)
            if reason:
                skipped.append(Attempt(provider=m.provider, model=m.id, outcome=f"skipped:{reason}"))
            else:
                pool.append(m)

        def price(m: CatalogModel) -> float:
            return m.cost(input_tokens, request.max_tokens)

        same_tier = [m for m in pool if m.tier == request.tier]
        proven, demoted, decision_info = self._rank_with_engine(same_tier, request, input_tokens)
        untested = sorted((m for m in same_tier if m.id not in proven and m.id not in demoted),
                          key=lambda m: (price(m), m.id))
        by_id = {m.id: m for m in pool}
        ordered: list[Candidate] = [Candidate(by_id[i].provider, i, "proven") for i in proven]
        if untested and proven and self._rng.random() < self.settings.explore_rate:
            pick = min(untested, key=lambda m: (self._graded(request.task_family, m.id), price(m)))
            ordered.insert(0, Candidate(pick.provider, pick.id, "explore"))
            untested = [m for m in untested if m.id != pick.id]
        ordered += [Candidate(m.provider, m.id, "untested") for m in untested]
        ordered += [Candidate(by_id[i].provider, i, "proven_poor") for i in demoted]
        higher = sorted((m for m in pool if m.tier != request.tier),
                        key=lambda m: (TIER_RANK[m.tier], price(m), m.id))
        ordered += [Candidate(m.provider, m.id, "stronger_tier") for m in higher]
        return ordered, skipped, decision_info

    def _graded(self, family: str, model_id: str) -> int:
        ev = self.ledger.evidence(family, model_id)
        return ev.samples if ev else 0

    def _rank_with_engine(self, models: list[CatalogModel], request: CompletionRequest,
                          input_tokens: int) -> tuple[list[str], list[str], dict]:
        if not models:
            return [], [], {"status": "NO_CANDIDATES"}
        now = int(self._clock())
        s = self.settings
        strategy = request.strategy or s.strategy
        engine_models = []
        for m in models:
            engine_models.append(Model(
                id=m.id, provider=m.provider, version=self.catalog.version, approved=True,
                provider_approved=True, enabled=True, capabilities=ALL_CAPABILITIES,
                modalities=frozenset({"text"}), context_limit=m.context_limit,
                output_limit=m.output_limit, regions=frozenset({"global"}),
                residencies=frozenset({"global"}),
                price=Price(version=self.catalog.version, expires_at=now + 86400, currency="USD",
                            input_per_million=Decimal(str(m.input)),
                            output_per_million=Decimal(str(m.output)),
                            cached_per_million=Decimal(str(m.cached))),
                evidence=self.ledger.evidence(request.task_family, m.id)))
        registry = Snapshot(version=self.catalog.version, expires_at=now + 86400,
                            models=tuple(engine_models))
        auth = Authorization(
            tenant_ref=hashlib.sha256(b"aj-llm-layer-gateway").hexdigest(), version="gateway-v1",
            expires_at=now + 86400, region="global", residency="global",
            privacy_class="personal" if request.data_class == "personal" else "non_personal",
            allowed_models=frozenset(m.id for m in models),
            allowed_strategies=frozenset(STRATEGIES), budget_remaining=Decimal("1000"),
            currency="USD", privacy_available=True, budget_available=True, feature_enabled=True)
        policy = Policy(version="gateway-v1", strategy=strategy, approved=True, enabled=True,
                        min_quality=s.min_quality, min_samples=s.min_samples,
                        max_evidence_age=s.max_evidence_age_days * 86400,
                        max_latency_ms=s.max_latency_ms, max_failure=s.max_failure, retries=1)
        task = {"families": (request.task_family,), "input_tokens": input_tokens,
                "output_tokens": request.max_tokens, "structured": request.json_mode,
                "criticality": "normal"}
        decision = route(task, registry, auth, policy, now=now, correlation_id="gateway")
        proven = [r.model_id for r in decision.eligible]
        demoted = [mid for mid, reasons in decision.excluded.items()
                   if "INSUFFICIENT_EVIDENCE" not in reasons and DEMOTE & set(reasons)]
        info = {"status": decision.status, "strategy": strategy,
                "excluded": {k: list(v) for k, v in decision.excluded.items()}}
        return proven, demoted, info
