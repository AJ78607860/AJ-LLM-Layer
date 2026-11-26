"""Shared HTTP plumbing for provider adapters (httpx, no vendor SDKs)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from ..errors import ErrorKind, ProviderError
from ..types import CompletionRequest, Usage


@dataclass(frozen=True)
class RawCompletion:
    text: str
    finish_reason: str
    usage: Usage


class Provider(Protocol):
    name: str

    def complete(self, request: CompletionRequest, model: str) -> RawCompletion: ...


def post_json(client: httpx.Client, provider: str, url: str, *, headers: dict, body: dict,
              timeout: float, classify) -> dict:
    """POST and return parsed JSON, or raise a classified ProviderError."""
    try:
        response = client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError(ErrorKind.TIMEOUT, provider, "request timed out") from None
    except httpx.HTTPError:
        raise ProviderError(ErrorKind.UNAVAILABLE, provider, "network error") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        raise classify(response.status_code, payload if isinstance(payload, dict) else {})
    if not isinstance(payload, dict):
        raise ProviderError(ErrorKind.UNAVAILABLE, provider, "malformed response", response.status_code)
    return payload


def generic_status(provider: str, status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(ErrorKind.AUTH, provider, "authentication failed", status)
    if status == 429:
        return ProviderError(ErrorKind.RATE_LIMIT, provider, "rate limited", status)
    if status == 408:
        return ProviderError(ErrorKind.TIMEOUT, provider, "provider timeout", status)
    if status >= 500:
        return ProviderError(ErrorKind.UNAVAILABLE, provider, "provider error", status)
    return ProviderError(ErrorKind.BAD_REQUEST, provider, "request rejected", status)
