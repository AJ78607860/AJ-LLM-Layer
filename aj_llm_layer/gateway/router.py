"""The execution gateway: one call in, the right model out, with fallback.

Selection modes (`AJ_LLM_SELECTION`):

* `routed` (default) — provider-agnostic. The selector (selector.py) shortlists
  every catalogued model that may take this request and orders them by evidence
  of correct answers and price; the gateway tries them in that order.
* `ordered` — legacy fixed provider order (`AJ_LLM_PROVIDER_ORDER`).

Whatever the mode, each attempt:
- skips providers that may not see this data, have no key, are benched for being
  out of credit / bad key, or are over their spend limit (health.py);
- retries once on timeouts/5xx, then falls back on anything another model could
  fix (out of credit, rate limit, bad key, outage, unknown model);
- returns bad requests and refusals straight away;
- records content-free evidence (outcome, latency, cost) per task family and model.

Logs carry only metadata. Never prompts, outputs or keys.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

import httpx

from .catalog import Catalog, load_catalog
from .config import GatewayConfig, ProviderConfig, load_config
from .errors import FALLBACK_KINDS, RETRY_KINDS, ErrorKind, GatewayError, ProviderError
from .evidence import EvidenceLedger
from .health import MemoryStore, ProviderHealth, build_store
from .providers import AnthropicProvider, OpenAIProvider, Provider
from .selector import Candidate, Selector, SelectorSettings
from .types import Attempt, CompletionRequest, CompletionResult
from .understand import resolve

log = logging.getLogger("aj.llm_layer.gateway")

_FACTORIES = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}
_FALLBACK_SKIPS = ("skipped:cooling_down", "skipped:spend_limit_reached", "skipped:provider_failed")


class Gateway:
    def __init__(self, config: GatewayConfig | None = None, *,
                 providers: dict[str, Provider] | None = None,
                 http_client: httpx.Client | None = None,
                 health: ProviderHealth | None = None,
                 catalog: Catalog | None = None,
                 settings: SelectorSettings | None = None,
                 ledger: EvidenceLedger | None = None,
                 retry_delay: float = 0.5,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] | None = None,
                 rng=None):
        from_env = config is None
        self.config = config or load_config()
        wall = wall_clock or time.time
        store = build_store(dict(os.environ)) if from_env and health is None else None
        self.health = health or ProviderHealth(store or MemoryStore(),
                                               spend_limits=self.config.spend_limits, clock=wall)
        self.catalog = catalog or load_catalog(None if from_env else {})
        self.ledger = ledger or EvidenceLedger(self.health.store, catalog_version=self.catalog.version,
                                               clock=wall)
        self._clock = clock
        self._retry_delay = retry_delay
        self._configs: dict[str, ProviderConfig] = {p.name: p for p in self.config.providers}
        if providers is not None:
            self._providers = dict(providers)
        else:
            client = http_client or httpx.Client()
            self._providers = {}
            for p in self.config.providers:
                if p.api_key and p.name in _FACTORIES:
                    kwargs = {"client": client}
                    if p.base_url:
                        kwargs["base_url"] = p.base_url
                    self._providers[p.name] = _FACTORIES[p.name](p.api_key, **kwargs)
        self.selector = Selector(self.catalog, self.ledger, self.health, self._configs,
                                 set(self._providers),
                                 settings or SelectorSettings.from_env(None if from_env else {}),
                                 clock=wall, rng=rng)

    # -- candidate selection -------------------------------------------------
    def _candidates(self, request: CompletionRequest) -> tuple[list[Candidate], list[Attempt], dict]:
        if request.model is not None:
            name = request.providers[0].lower()
            reason = self._provider_gate(name, request)
            if reason:
                return [], [Attempt(provider=name, model=request.model, outcome=f"skipped:{reason}")], {}
            return [Candidate(name, request.model, "pinned")], [], {}
        if self.config.selection == "routed":
            return self.selector.shortlist(request)
        return self._ordered(request)

    def _provider_gate(self, name: str, request: CompletionRequest) -> str | None:
        cfg = self._configs.get(name)
        if cfg is None:
            return "provider_not_configured"
        if request.data_class == "personal" and not cfg.personal_data_approved:
            return "personal_data_not_approved"
        if name not in self._providers:
            return "no_api_key"
        return self.health.blocked(name)

    def _ordered(self, request: CompletionRequest):
        order = [p.name for p in self.config.providers]
        if request.providers:
            wanted = [p.lower() for p in request.providers]
            order = [p for p in wanted if p in order]
        usable, skipped = [], []
        for name in order:
            model = (self._configs[name].models.get(request.tier)
                     or self.catalog.default_for(name, request.tier) or "")
            reason = self._provider_gate(name, request) or (None if model else "no_model_for_tier")
            if reason:
                skipped.append(Attempt(provider=name, model=model, outcome=f"skipped:{reason}"))
            else:
                usable.append(Candidate(name, model, "ordered"))
        return usable, skipped, {}

    # -- execution -----------------------------------------------------------
    def resolve(self, request: CompletionRequest) -> tuple[CompletionRequest, dict]:
        """Fill in task_family / tier the app left as "auto" by reading the question."""
        return resolve(request)

    def complete(self, request: CompletionRequest, *, app: str = "unknown") -> CompletionResult:
        started = self._clock()
        request, understanding = self.resolve(request)
        candidates, attempts, _ = self._candidates(request)
        family = request.task_family
        last: ProviderError | None = None
        held: CompletionResult | None = None   # answer that failed an objective check
        spent: float | None = None             # every billed attempt in this call, not just the last
        escalate_above: str | None = None      # tier of that answer; only stronger tiers next
        tier_of = {m.id: m.tier for m in self.catalog.models}
        rank = {"fast": 0, "standard": 1, "smart": 2}
        for cand in candidates:
            name, model = cand.provider, cand.model
            if escalate_above is not None and rank.get(tier_of.get(model, ""), -1) <= rank[escalate_above]:
                continue
            blocked = self.health.blocked(name)
            if blocked:
                # The provider ran out of credit / failed auth on an earlier model in this call.
                attempts.append(Attempt(provider=name, model=model, outcome=f"skipped:{blocked}"))
                continue
            provider = self._providers[name]
            for attempt_no in (1, 2):
                t0 = self._clock()
                try:
                    raw = provider.complete(request, model)
                except ProviderError as exc:
                    elapsed = int((self._clock() - t0) * 1000)
                    attempts.append(Attempt(provider=name, model=model, outcome=exc.kind.value,
                                            latency_ms=elapsed))
                    last = exc
                    self.health.record_failure(name, exc.kind)
                    if exc.kind not in (ErrorKind.BAD_REQUEST, ErrorKind.REFUSED):
                        self.ledger.record_attempt(family, model, ok=False,
                                                   timeout=exc.kind is ErrorKind.TIMEOUT,
                                                   latency_ms=elapsed, cost_usd=None)
                    if exc.kind in RETRY_KINDS and attempt_no == 1:
                        if self._retry_delay:
                            time.sleep(self._retry_delay)
                        continue
                    break
                elapsed = int((self._clock() - t0) * 1000)
                attempts.append(Attempt(provider=name, model=model, outcome="ok", latency_ms=elapsed))
                cost = self._cost(model, raw.usage)
                if cost is not None:
                    spent = (spent or 0.0) + cost
                self.health.record_success(name, cost)
                self.ledger.record_attempt(family, model, ok=True, timeout=False,
                                           latency_ms=elapsed, cost_usd=cost)
                call_id = self.ledger.new_call(family=family, model=model, provider=name, app=app)
                failed_check = not raw.text.strip() or (request.json_mode and not _is_json(raw.text))
                if failed_check:
                    self.ledger.grade_call(call_id, False, app=app)  # objective check failed
                fallback = any(a.outcome != "ok" and (not a.outcome.startswith("skipped:")
                                                      or a.outcome.startswith(_FALLBACK_SKIPS))
                               for a in attempts)
                result = CompletionResult(
                    text=raw.text, provider=name, model=model, finish_reason=raw.finish_reason,
                    usage=raw.usage, cost_usd=None if spent is None else round(spent, 8),
                    latency_ms=int((self._clock() - started) * 1000),
                    fallback_used=fallback, attempts=tuple(attempts), call_id=call_id,
                    selection=cand.selection if held is None else "escalated",
                    task_family=family, tier=request.tier, understanding=understanding)
                if failed_check and model in tier_of:
                    # Empty or not the JSON asked for: try a stronger model, keep this as a last resort.
                    attempts[-1] = Attempt(provider=name, model=model, outcome="failed_check",
                                           latency_ms=elapsed)
                    held = held or result.model_copy(update={"attempts": tuple(attempts)})
                    escalate_above = tier_of[model]
                    break
                self._log(app, request, result.attempts, "ok", result)
                return result
            if last is not None and last.kind not in FALLBACK_KINDS and escalate_above is None:
                break  # bad request / refusal: another model won't help
        if held is not None:
            held = held.model_copy(update={"attempts": tuple(attempts),
                                           "cost_usd": None if spent is None else round(spent, 8)})
            self._log(app, request, held.attempts, "ok", held)
            return held
        if last is not None:
            kind, message = last.kind, f"all providers failed (last: {last})"
        else:
            reasons = [self.health.bench_reason(a.provider) for a in attempts
                       if a.outcome.startswith("skipped:cooling_down")]
            if any(a.outcome == "skipped:spend_limit_reached" for a in attempts):
                reasons.append(ErrorKind.QUOTA)
            kind = next((k for k in reasons if k), ErrorKind.NOT_CONFIGURED)
            message = ("no provider is available for this request: "
                       + ", ".join(f"{a.provider}/{a.model} {a.outcome.removeprefix('skipped:')}"
                                   for a in attempts))
        self._log(app, request, tuple(attempts), kind.value, None)
        raise GatewayError(kind, tuple(attempts), message)

    def feedback(self, call_id: str, correct: bool, *, app: str) -> dict:
        call = self.ledger.grade_call(call_id, correct, app=app)
        log.info("llm_feedback app=%s family=%s model=%s correct=%s",
                 app, call["family"], call["model"], correct)
        return {"call_id": call_id, "model": call["model"], "task_family": call["family"],
                "recorded": True}

    def leaderboard(self, family: str | None = None) -> dict:
        families = [family] if family else (self.ledger.families() or ["conversation"])
        board = {}
        for fam in families:
            rows = [{"model": m.id, "provider": m.provider, "tier": m.tier,
                     "price_in_per_m": m.input, "price_out_per_m": m.output,
                     "price_verified": m.price_verified, **self.ledger.summary(fam, m.id)}
                    for m in self.catalog.models]
            rows.sort(key=lambda r: (-(r["accuracy_lower_bound"] or 0),
                                     r["cost_per_correct_usd"] if r["cost_per_correct_usd"] is not None else 1e9,
                                     r["model"]))
            board[fam] = rows
        return {"catalog_version": self.catalog.version, "families": board}

    def _cost(self, model: str, usage) -> float | None:
        price = self.config.prices.get(model)  # explicit override wins
        if price:
            return round((usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1_000_000, 8)
        entry = self.catalog.get(model)
        return entry.cost(usage.input_tokens, usage.output_tokens) if entry else None

    def status(self) -> dict:
        status = self.config.status()
        for name, info in status.items():
            info.update(self.health.status(name))
            info["catalog_models"] = [m.id for m in self.catalog.models if m.provider == name]
        return status

    @staticmethod
    def _log(app, request, attempts, outcome, result) -> None:
        trail = ",".join(f"{a.provider}/{a.model}:{a.outcome}" for a in attempts)
        if result is None:
            log.warning("llm_call app=%s purpose=%s family=%s tier=%s outcome=%s attempts=%s",
                        app, request.purpose, request.task_family, request.tier, outcome, trail)
        else:
            log.info("llm_call app=%s purpose=%s family=%s tier=%s outcome=ok provider=%s model=%s "
                     "selection=%s latency_ms=%d in_tokens=%d out_tokens=%d fallback=%s attempts=%s",
                     app, request.purpose, request.task_family, request.tier, result.provider,
                     result.model, result.selection, result.latency_ms, result.usage.input_tokens,
                     result.usage.output_tokens, result.fallback_used, trail)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False
