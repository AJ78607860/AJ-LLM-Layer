"""Atomic shared nonce registry. Stores nonce digests only, never credentials.

Redis must be dedicated and authenticated/TLS. It should also be non-evicting and durable, but the registry no
longer depends on that for safety: an **epoch marker** lives inside the same sorted set (score +inf, so expiry
cleanup never removes it). If Redis restarts without persistence, is flushed, or evicts the key, the marker goes
with the nonces. The next check recreates it and, for one credential lifetime plus clock skew
(``QUARANTINE_SECONDS``), refuses every credential that could have been issued before that moment (503): a nonce
that was seen before the loss can never be accepted again. The key has no TTL, so ``volatile-*`` eviction
policies cannot select it.
"""
from functools import lru_cache
import hashlib
import os
import re
from urllib.parse import urlsplit

from .service_auth import MAX_LIFETIME_SECONDS, ReplayCache, SKEW_SECONDS

# A credential with exp < epoch + this could have been issued (iat <= exp - MAX_LIFETIME) before the epoch, by an
# issuer clock up to SKEW_SECONDS ahead of Redis.
QUARANTINE_SECONDS = MAX_LIFETIME_SECONDS + SKEW_SECONDS


class ReplayUnavailable(Exception):
    """Fixed public error; internal connection information is never propagated."""


# One bounded sorted set; Lua serializes the epoch check, expiry, duplicate detection and capacity.
# Returns 1 new, 0 replay, -1 full, -2 expired, -3 too far ahead, -4 epoch quarantine, -5 corrupt marker.
# Redis server time avoids relying on clocks shared by separate receiver hosts.
SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1])
local expires = tonumber(ARGV[2])
if expires <= now then return -2 end
if expires - now > 90 then return -3 end
local marker = redis.call('ZRANGEBYSCORE', KEYS[1], '+inf', '+inf', 'LIMIT', 0, 1)
local epoch
if #marker == 0 then
  epoch = now
  redis.call('ZADD', KEYS[1], '+inf', 'epoch:' .. now)
else
  epoch = tonumber(string.sub(marker[1], 7))
  if epoch == nil then return -5 end
end
if expires < epoch + tonumber(ARGV[4]) then return -4 end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
if redis.call('ZCARD', KEYS[1]) - 1 >= tonumber(ARGV[3]) then return -1 end
redis.call('ZADD', KEYS[1], expires + 30, ARGV[1])
return 1
"""


class RedisReplayStore:
    def __init__(self, client, *, environment: str, capacity: int = 10000):
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", environment) or not 1 <= capacity <= 100000:
            raise ReplayUnavailable()
        self.client = client
        self.key = 'aj:llm:replay:v1:' + environment
        self.capacity = capacity

    def add_if_absent(self, jti: str, expires_at: int, now: int) -> bool:
        # Retain beyond strict exp to cover differing receiver clocks. Never evict live
        # nonces to admit new work. Capacity exhaustion and Redis errors return 503.
        ttl = expires_at - now + SKEW_SECONDS
        if not 1 <= ttl <= 120:
            raise ReplayUnavailable()
        digest = hashlib.sha256(jti.encode('ascii')).hexdigest()
        try:
            result = self.client.eval(SCRIPT, 1, self.key, digest, expires_at, self.capacity,
                                     QUARANTINE_SECONDS)
        except Exception:
            raise ReplayUnavailable() from None
        if result == -2:
            from .service_auth import InvalidCredential
            raise InvalidCredential('expired')
        if result not in (0, 1):
            # -4: the registry was lost (restart without persistence, flush, eviction) less than one credential
            # lifetime ago, so this credential might already have been used. -1/-3/-5 and anything else: fail closed.
            raise ReplayUnavailable()
        return result == 1


@lru_cache(maxsize=8)
def _redis(url: str, environment: str):
    from redis import Redis
    return RedisReplayStore(Redis.from_url(
        url, socket_connect_timeout=0.5, socket_timeout=0.5,
        max_connections=16, retry_on_timeout=False, decode_responses=False,
    ), environment=environment)


_local = ReplayCache()


def get_replay_store():
    env = (os.getenv('AJ_ENVIRONMENT') or '').strip().lower()
    backend = os.getenv('AJ_LLM_LAYER_REPLAY_BACKEND', 'redis')
    if backend == 'local' and env in ('test', 'local', 'development'):
        return _local
    if backend != 'redis':
        raise ReplayUnavailable()
    url = os.getenv('AJ_LLM_LAYER_REPLAY_REDIS_URL', '')
    try:
        parsed = urlsplit(url)
        # Loopback cleartext only for explicit local synthetic integration tests.
        local_test = env in ('test', 'local', 'development') and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if (parsed.scheme != 'rediss' and not (parsed.scheme == 'redis' and local_test)) or not parsed.hostname:
            raise ReplayUnavailable()
        if not local_test and not parsed.password:
            raise ReplayUnavailable()
        return _redis(url, env)
    except Exception:
        raise ReplayUnavailable() from None
