"""The execution gateway: one call in, best available provider out.

Order of work for each request:
1. Build the candidate list from the configured preference order (or the
   request's own `providers` list, which can only narrow it).
2. Drop providers that may not see this data (personal data → approved only),
   that have no key or no model for the tier, or that are cooling down after
   running out of credits / failing auth.
3. Try each in turn: one quick retry on timeouts/5xx, then fall back on any
   failure another provider could fix (quota, rate limit, auth, outage).
   Bad requests and refusals are returned straight away.

Logs carry only metadata: app, purpose, provider, model, outcome, latency,
token counts. Never prompts, outputs or keys.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import httpx

from .config import GatewayConfig, ProviderConfig, load_config
from .errors import (FALLBACK_KINDS, RETRY_KINDS, TRIP_KINDS, ErrorKind, GatewayError,
                     ProviderError)
from .providers import AnthropicProvider, OpenAIProvider, Provider
from .types import Attempt, CompletionRequest, CompletionResult

log = logging.getLogger("aj.llm_layer.gateway")

_FACTORIES = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}


class CircuitBreaker:
    """Takes a provider out of rotation for a while after quota/auth/rate-limit failures."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until: dict[str, tuple[float, ErrorKind]] = {}
        self._lock = threading.Lock()

    def open_for(self, provider: str) -> float:
        with self._lock:
            until, _ = self._until.get(provider, (0.0, None))
            return max(0.0, until - self._clock())

    def reason(self, provider: str) -> ErrorKind | None:
        with self._lock:
            return self._until.get(provider, (0.0, None))[1]

    def trip(self, provider: str, seconds: float, kind: ErrorKind = ErrorKind.UNAVAILABLE) -> None:
        with self._lock:
            until = self._clock() + seconds
            current = self._until.get(provider)
            if current is None or until >= current[0]:
                self._until[provider] = (until, kind)

    def reset(self, provider: str) -> None:
        with self._lock:
            self._until.pop(provider, None)


class Gateway:
    def __init__(self, config: GatewayConfig | None = None, *,
                 providers: dict[str, Provider] | None = None,
                 http_client: httpx.Client | None = None,
                 breaker: CircuitBreaker | None = None,
                 retry_delay: float = 0.5,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config or load_config()
        self.breaker = breaker or CircuitBreaker(clock)
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

    # -- candidate selection -------------------------------------------------
    def _candidates(self, request: CompletionRequest) -> tuple[list[tuple[str, str]], list[Attempt]]:
        order = [p.name for p in self.config.providers]
        if request.providers:
            wanted = [p.lower() for p in request.providers]
            order = [p for p in wanted if p in order]
        usable, skipped = [], []
        for name in order:
            cfg = self._configs[name]
            model = request.model or cfg.models.get(request.tier, "")
            if request.data_class == "personal" and not cfg.personal_data_approved:
                skipped.append(Attempt(provider=name, model=model, outcome="skipped:personal_data_not_approved"))
            elif name not in self._providers:
                skipped.append(Attempt(provider=name, model=model, outcome="skipped:no_api_key"))
            elif not model:
                skipped.append(Attempt(provider=name, model="", outcome="skipped:no_model_for_tier"))
            elif self.breaker.open_for(name) > 0:
                reason = self.breaker.reason(name)
                skipped.append(Attempt(provider=name, model=model,
                                       outcome=f"skipped:cooling_down:{reason.value if reason else 'error'}"))
            else:
                usable.append((name, model))
        return usable, skipped

    # -- execution -----------------------------------------------------------
    def complete(self, request: CompletionRequest, *, app: str = "unknown") -> CompletionResult:
        started = self._clock()
        candidates, attempts = self._candidates(request)
        last: ProviderError | None = None
        for index, (name, model) in enumerate(candidates):
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
                    if exc.kind in TRIP_KINDS:
                        self.breaker.trip(name, TRIP_KINDS[exc.kind], exc.kind)
                    if exc.kind in RETRY_KINDS and attempt_no == 1:
                        if self._retry_delay:
                            time.sleep(self._retry_delay)
                        continue
                    break
                elapsed = int((self._clock() - t0) * 1000)
                attempts.append(Attempt(provider=name, model=model, outcome="ok", latency_ms=elapsed))
                self.breaker.reset(name)
                result = CompletionResult(
                    text=raw.text, provider=name, model=model, finish_reason=raw.finish_reason,
                    usage=raw.usage, cost_usd=self._cost(model, raw.usage),
                    latency_ms=int((self._clock() - started) * 1000),
                    fallback_used=index > 0, attempts=tuple(attempts))
                self._log(app, request, result.attempts, "ok", result)
                return result
            if last is not None and last.kind not in FALLBACK_KINDS:
                break  # bad request / refusal: another provider won't help
        if last is not None:
            kind, message = last.kind, f"all providers failed (last: {last})"
        else:
            cooling = [self.breaker.reason(a.provider) for a in attempts
                       if a.outcome.startswith("skipped:cooling_down")]
            kind = next((k for k in cooling if k), ErrorKind.NOT_CONFIGURED)
            message = ("no provider is available for this request: "
                       + ", ".join(f"{a.provider} {a.outcome.removeprefix('skipped:')}" for a in attempts))
        self._log(app, request, tuple(attempts), kind.value, None)
        raise GatewayError(kind, tuple(attempts), message)

    def _cost(self, model: str, usage) -> float | None:
        price = self.config.prices.get(model)
        if not price:
            return None
        return round((usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1_000_000, 8)

    def status(self) -> dict:
        status = self.config.status()
        for name, info in status.items():
            info["cooling_down_seconds"] = round(self.breaker.open_for(name), 1)
        return status

    @staticmethod
    def _log(app, request, attempts, outcome, result) -> None:
        trail = ",".join(f"{a.provider}:{a.outcome}" for a in attempts)
        if result is None:
            log.warning("llm_call app=%s purpose=%s tier=%s outcome=%s attempts=%s",
                        app, request.purpose, request.tier, outcome, trail)
        else:
            log.info("llm_call app=%s purpose=%s tier=%s outcome=ok provider=%s model=%s "
                     "latency_ms=%d in_tokens=%d out_tokens=%d fallback=%s attempts=%s",
                     app, request.purpose, request.tier, result.provider, result.model,
                     result.latency_ms, result.usage.input_tokens, result.usage.output_tokens,
                     result.fallback_used, trail)
