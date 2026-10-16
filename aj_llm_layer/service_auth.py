"""Shared signed service-credential verifier for AJ LLM Layer (receiver).

Design: docs/llm-layer-service-credentials-design.md. Standard library only; mirrors
browser_service/protocol.py. A fresh token is minted per request:

    X-AJ-Service-Auth: v1.<kid>.<b64url(claims)>.<b64url(HMAC-SHA256(key, "aj-svc-v1|<kid>.<claims_b64>"))>

Claims: iss, aud, env, iat, exp (iat+30, cap 60), jti (128-bit random), scope, m (method),
p (path), bh (b64url SHA-256 of the exact body bytes). No user, prompt or correlation data.

Keys come from AJ_LLM_LAYER_SIGNING_KEYS ("kid:key,kid:key"; key >= 32 chars, e.g.
secrets.token_urlsafe(48)) and AJ_LLM_LAYER_SIGNING_KID. Keys and tokens are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Optional

VERSION = "v1"
HEADER = "X-AJ-Service-Auth"
CONTEXT = b"aj-svc-v1|"
ISSUER = "aj-assistant"
AUDIENCE = "aj-llm-layer"
DEFAULT_LIFETIME_SECONDS = 30
MAX_LIFETIME_SECONDS = 60
SKEW_SECONDS = 30
MIN_KEY_CHARS = 32
MODES = ("static", "both", "signed")
_KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class SigningUnavailable(Exception):
    """No usable signing key/kid: the caller must not send (no silent downgrade)."""


def auth_mode() -> str:
    """AJ_LLM_LAYER_AUTH_MODE: static (default, unchanged behaviour) | both | signed."""
    mode = (os.getenv("AJ_LLM_LAYER_AUTH_MODE") or "static").strip().lower()
    return mode if mode in MODES else "static"


def parse_keys(raw: str) -> dict[str, bytes]:
    """'kid:key,kid2:key2' -> {kid: key bytes}. Invalid entries are dropped (never echoed)."""
    keys: dict[str, bytes] = {}
    for entry in (raw or "").split(","):
        kid, sep, key = entry.strip().partition(":")
        if not (sep and _KID_RE.fullmatch(kid) and MIN_KEY_CHARS <= len(key) <= 256
                and key.isascii() and not any(c.isspace() for c in key)) or kid in keys:
            return {}  # ambiguous or partially invalid rotation configuration fails closed
        keys[kid] = key.encode("ascii")
    return keys


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def body_hash(body: bytes) -> str:
    return b64(hashlib.sha256(body).digest())


def mac(key: bytes, kid: str, claims_b64: str) -> str:
    return b64(hmac.new(key, CONTEXT + f"{kid}.{claims_b64}".encode("ascii"), hashlib.sha256).digest())


def canonical(claims: dict) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(*, method: str, path: str, body: bytes, scope: str, key: bytes, kid: str, env: str,
         now: Optional[float] = None, lifetime: int = DEFAULT_LIFETIME_SECONDS, jti: Optional[str] = None) -> str:
    if not _KID_RE.fullmatch(kid) or len(key) < MIN_KEY_CHARS:
        raise SigningUnavailable("key")
    iat = int(time.time() if now is None else now)
    lifetime = max(1, min(int(lifetime), MAX_LIFETIME_SECONDS))
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "env": env, "iat": iat, "exp": iat + lifetime,
        "jti": jti or b64(secrets.token_bytes(16)), "scope": scope,
        "m": method.upper(), "p": path, "bh": body_hash(body),
    }
    claims_b64 = b64(canonical(claims))
    return f"{VERSION}.{kid}.{claims_b64}.{mac(key, kid, claims_b64)}"


def sign_from_env(*, method: str, path: str, body: bytes, scope: str) -> str:
    keys = parse_keys(os.getenv("AJ_LLM_LAYER_SIGNING_KEYS", ""))
    kid = (os.getenv("AJ_LLM_LAYER_SIGNING_KID") or "").strip()
    if kid not in keys:
        raise SigningUnavailable("kid")
    env = (os.getenv("AJ_ENVIRONMENT") or "").strip().lower()
    if not env:
        raise SigningUnavailable("env")
    return sign(method=method, path=path, body=body, scope=scope, key=keys[kid], kid=kid, env=env)


# ---------------------------------------------------------------- verification
# Reference verifier. The AJ LLM Layer carries the same function (aj_llm_layer/service_auth.py);
# tests/fixtures/llm_layer_service_auth_vectors.json is committed to both repos so they cannot drift.

MAX_HEADER_CHARS = 1024
REASONS = ("format", "kid_unknown", "claims", "not_yet_valid", "expired", "lifetime", "mac", "iss", "aud",
           "env", "scope", "method", "path", "body_hash", "replay")


class InvalidCredential(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # one of REASONS; safe to log


class ReplayCache:
    """In-memory single-use jti store (one process). Entries live until exp + skew; bounded size."""

    def __init__(self, max_entries: int = 10000):
        import threading
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def add_if_absent(self, jti: str, expires_at: int, now: int) -> bool:
        with self._lock:
            if len(self._seen) >= self._max:
                for key in [k for k, v in self._seen.items() if v < now]:
                    del self._seen[key]
                if len(self._seen) >= self._max:
                    return False  # full of live entries: refuse (fail closed)
            if jti in self._seen and self._seen[jti] >= now:
                return False
            self._seen[jti] = expires_at + SKEW_SECONDS
            return True


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify(header: str, *, method: str, path: str, body: bytes, keys: dict[str, bytes], env: str,
           scope: str, replay: ReplayCache, now: Optional[float] = None) -> dict:
    """Claims on success, else InvalidCredential(reason). Cheap checks first; MAC compared in constant time."""
    now_i = int(time.time() if now is None else now)
    # ASCII only: WSGI decodes headers as latin-1, and a non-ASCII kid/claims/signature would otherwise raise
    # UnicodeEncodeError (mac) or TypeError (compare_digest) -> HTTP 500 instead of a clean rejection.
    if not isinstance(header, str) or not header or len(header) > MAX_HEADER_CHARS or not header.isascii():
        raise InvalidCredential("format")
    parts = header.split(".")
    if len(parts) != 4 or parts[0] != VERSION or not _KID_RE.fullmatch(parts[1]):
        raise InvalidCredential("format")
    _, kid, claims_b64, sig = parts
    key = keys.get(kid)
    if key is None:
        raise InvalidCredential("kid_unknown")
    if not hmac.compare_digest(mac(key, kid, claims_b64), sig):
        raise InvalidCredential("mac")
    try:
        def unique(pairs):
            obj = {}
            for name, value in pairs:
                if name in obj:
                    raise ValueError("duplicate claim")
                obj[name] = value
            return obj
        claims = json.loads(_unb64(claims_b64), object_pairs_hook=unique)
        iat, exp, jti = claims["iat"], claims["exp"], claims["jti"]
    except Exception:
        raise InvalidCredential("claims") from None
    if (not isinstance(claims, dict)
            or set(claims) != {"iss", "aud", "env", "iat", "exp", "jti", "scope", "m", "p", "bh"}
            or type(iat) is not int or type(exp) is not int or iat < 0
            or not isinstance(jti, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", jti)):
        raise InvalidCredential("claims")
    if iat > now_i + SKEW_SECONDS:
        raise InvalidCredential("not_yet_valid")
    if exp <= iat or exp - iat > MAX_LIFETIME_SECONDS:
        raise InvalidCredential("lifetime")
    if now_i >= exp:
        raise InvalidCredential("expired")
    for name, expected, reason in (("iss", ISSUER, "iss"), ("aud", AUDIENCE, "aud"), ("env", env, "env"),
                                   ("scope", scope, "scope"), ("m", method.upper(), "method"), ("p", path, "path")):
        if claims.get(name) != expected:
            raise InvalidCredential(reason)
    bh = claims.get("bh")
    if not isinstance(bh, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", bh):
        raise InvalidCredential("body_hash")
    if not hmac.compare_digest(bh, body_hash(body)):
        raise InvalidCredential("body_hash")
    if not replay.add_if_absent(jti, exp, now_i):
        raise InvalidCredential("replay")
    return claims
