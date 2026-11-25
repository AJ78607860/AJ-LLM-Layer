"""Provider health and credit awareness: keep calls away from providers that can't pay.

Two layers of protection:

1. **Reactive (always on).** When a provider says it is out of credits (or its key
   is rejected), it is benched. A repeat failure benches it for longer:
   10 min → 30 min → 2 h → 6 h. One successful call clears it. Rate limits bench
   for 20 s without escalating.
2. **Proactive (optional).** `AJ_LLM_SPEND_LIMITS='{"openai": 20, "anthropic": 50}'`
   (USD per calendar month, UTC). Spend is counted from token usage × `AJ_LLM_PRICES`.
   Once a provider reaches its limit it is skipped *before* it runs dry, so users
   never see the failed call. Set the limit a little under the prepaid balance.

State is shared across workers and replicas when `AJ_LLM_STATE_REDIS_URL` is set,
otherwise kept in memory. If Redis is unreachable the gateway keeps working on
in-memory state; health tracking must never be the thing that stops a reply.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .errors import ErrorKind

log = logging.getLogger("aj.llm_layer.gateway.health")

QUOTA_BACKOFF = (600.0, 1800.0, 7200.0, 21600.0)
BACKOFF = {ErrorKind.QUOTA: QUOTA_BACKOFF, ErrorKind.AUTH: QUOTA_BACKOFF,
           ErrorKind.RATE_LIMIT: (20.0,)}


@dataclass(frozen=True)
class Bench:
    kind: ErrorKind
    until: float
    strikes: int
    since: float


class Store(Protocol):
    def get_bench(self, provider: str) -> Bench | None: ...
    def set_bench(self, provider: str, bench: Bench) -> None: ...
    def clear_bench(self, provider: str) -> None: ...
    def add_spend(self, provider: str, month: str, usd: float) -> float: ...
    def get_spend(self, provider: str, month: str) -> float: ...


class MemoryStore:
    def __init__(self):
        self._bench: dict[str, Bench] = {}
        self._spend: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def get_bench(self, provider):
        with self._lock:
            return self._bench.get(provider)

    def set_bench(self, provider, bench):
        with self._lock:
            self._bench[provider] = bench

    def clear_bench(self, provider):
        with self._lock:
            self._bench.pop(provider, None)

    def add_spend(self, provider, month, usd):
        with self._lock:
            key = (provider, month)
            self._spend[key] = self._spend.get(key, 0.0) + usd
            return self._spend[key]

    def get_spend(self, provider, month):
        with self._lock:
            return self._spend.get((provider, month), 0.0)


class RedisStore:
    """Shared state. Every operation falls back to a local MemoryStore on Redis errors."""

    PREFIX = "aj-llm:gateway:"

    def __init__(self, client, *, environment: str = "default"):
        self._r = client
        self._p = f"{self.PREFIX}{environment}:"
        self._local = MemoryStore()

    def _safe(self, op, fallback):
        try:
            return op()
        except Exception as exc:  # redis outage must not stop replies
            log.warning("health_store_unavailable error=%s", type(exc).__name__)
            return fallback()

    def get_bench(self, provider):
        def op():
            raw = self._r.get(self._p + "bench:" + provider)
            if not raw:
                return None
            d = json.loads(raw)
            return Bench(ErrorKind(d["kind"]), float(d["until"]), int(d["strikes"]), float(d["since"]))
        return self._safe(op, lambda: self._local.get_bench(provider))

    def set_bench(self, provider, bench):
        self._local.set_bench(provider, bench)
        payload = json.dumps({"kind": bench.kind.value, "until": bench.until,
                              "strikes": bench.strikes, "since": bench.since})
        # Keep the record (and its strike count) a day past expiry so repeats escalate.
        ttl = int(max(60, bench.until - time.time() + 86400))
        self._safe(lambda: self._r.set(self._p + "bench:" + provider, payload, ex=ttl), lambda: None)

    def clear_bench(self, provider):
        self._local.clear_bench(provider)
        self._safe(lambda: self._r.delete(self._p + "bench:" + provider), lambda: None)

    def add_spend(self, provider, month, usd):
        key = f"{self._p}spend:{provider}:{month}"

        def op():
            total = float(self._r.incrbyfloat(key, usd))
            self._r.expire(key, 40 * 86400)
            return total
        local = self._local.add_spend(provider, month, usd)
        return self._safe(op, lambda: local)

    def get_spend(self, provider, month):
        def op():
            raw = self._r.get(f"{self._p}spend:{provider}:{month}")
            return float(raw) if raw else 0.0
        return self._safe(op, lambda: self._local.get_spend(provider, month))


class ProviderHealth:
    def __init__(self, store: Store | None = None, *, spend_limits: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store or MemoryStore()
        self.spend_limits = dict(spend_limits or {})
        self._clock = clock

    def month(self) -> str:
        return time.strftime("%Y-%m", time.gmtime(self._clock()))

    # -- bench ---------------------------------------------------------------
    def blocked(self, provider: str) -> str | None:
        """Why this provider must be skipped right now, or None if it's usable."""
        bench = self.store.get_bench(provider)
        if bench and bench.until > self._clock():
            return f"cooling_down:{bench.kind.value}"
        limit = self.spend_limits.get(provider)
        if limit is not None and self.store.get_spend(provider, self.month()) >= limit:
            return "spend_limit_reached"
        return None

    def bench_reason(self, provider: str) -> ErrorKind | None:
        bench = self.store.get_bench(provider)
        if bench and bench.until > self._clock():
            return bench.kind
        return None

    def record_failure(self, provider: str, kind: ErrorKind) -> None:
        schedule = BACKOFF.get(kind)
        if not schedule:
            return
        now = self._clock()
        previous = self.store.get_bench(provider)
        strikes = previous.strikes + 1 if previous and previous.kind == kind else 1
        seconds = schedule[min(strikes, len(schedule)) - 1]
        since = previous.since if previous and previous.kind == kind else now
        self.store.set_bench(provider, Bench(kind, now + seconds, strikes, since))
        if kind in (ErrorKind.QUOTA, ErrorKind.AUTH):
            log.warning("provider_benched provider=%s reason=%s strikes=%d seconds=%d",
                        provider, kind.value, strikes, seconds)

    def record_success(self, provider: str, cost_usd: float | None) -> None:
        if self.store.get_bench(provider) is not None:
            self.store.clear_bench(provider)
        if cost_usd:
            total = self.store.add_spend(provider, self.month(), cost_usd)
            limit = self.spend_limits.get(provider)
            if limit is not None and total >= limit:
                log.warning("provider_spend_limit_reached provider=%s month=%s", provider, self.month())

    def reset(self, provider: str) -> None:
        """Owner action after topping up credits or fixing a key."""
        self.store.clear_bench(provider)

    def status(self, provider: str) -> dict:
        now = self._clock()
        bench = self.store.get_bench(provider)
        active = bench is not None and bench.until > now
        out = {"available": self.blocked(provider) is None,
               "benched": {"reason": bench.kind.value, "strikes": bench.strikes,
                           "seconds_left": round(bench.until - now),
                           "since_unix": int(bench.since)} if active else None,
               "spend_this_month_usd": round(self.store.get_spend(provider, self.month()), 4)}
        if provider in self.spend_limits:
            out["spend_limit_usd"] = self.spend_limits[provider]
        return out


def build_store(env: dict[str, str]) -> Store:
    url = (env.get("AJ_LLM_STATE_REDIS_URL") or "").strip()
    if not url:
        return MemoryStore()
    try:
        import redis
        client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        return RedisStore(client, environment=(env.get("AJ_ENVIRONMENT") or "default").strip().lower())
    except Exception as exc:
        log.warning("health_store_unavailable error=%s; using memory", type(exc).__name__)
        return MemoryStore()
