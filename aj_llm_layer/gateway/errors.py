"""Normalised provider failures. The kind decides whether the gateway falls back."""
from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    QUOTA = "quota"              # out of credits / billing hard limit
    RATE_LIMIT = "rate_limit"    # 429 that will clear on its own
    AUTH = "auth"                # bad or missing API key
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"  # 5xx, overloaded, network failure
    BAD_REQUEST = "bad_request"  # our request is wrong; another provider won't help
    REFUSED = "refused"          # content policy refusal
    NOT_CONFIGURED = "not_configured"


# Failures where trying the next provider is worthwhile.
FALLBACK_KINDS = frozenset({ErrorKind.QUOTA, ErrorKind.RATE_LIMIT, ErrorKind.AUTH,
                            ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE, ErrorKind.NOT_CONFIGURED})
# Failures worth one quick retry on the same provider before moving on.
RETRY_KINDS = frozenset({ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE})
# Failures that take a provider out of rotation for a cool-down period.
TRIP_KINDS = {ErrorKind.QUOTA: 600.0, ErrorKind.AUTH: 600.0, ErrorKind.RATE_LIMIT: 20.0}


class ProviderError(Exception):
    """Raised by adapters. `detail` is a short fixed phrase, never provider text or content."""

    def __init__(self, kind: ErrorKind, provider: str, detail: str = "", status: int | None = None):
        self.kind = kind
        self.provider = provider
        self.detail = detail
        self.status = status
        super().__init__(f"{provider}: {kind.value}{' (' + detail + ')' if detail else ''}")


class GatewayError(Exception):
    """No provider produced an answer. Carries the content-free attempt trail."""

    def __init__(self, kind: ErrorKind, attempts: tuple, message: str):
        self.kind = kind
        self.attempts = attempts
        super().__init__(message)
