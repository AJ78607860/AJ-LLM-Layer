"""Anthropic Messages API adapter."""
from __future__ import annotations

import httpx

from ..errors import ErrorKind, ProviderError
from ..types import CompletionRequest, Usage
from .base import RawCompletion, generic_status, post_json

NAME = "anthropic"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"


def classify(status: int, payload: dict) -> ProviderError:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    etype = str(error.get("type") or "")
    message = str(error.get("message") or "").lower()
    if status == 402 or etype == "billing_error" or "credit balance" in message:
        return ProviderError(ErrorKind.QUOTA, NAME, "out of credits", status)
    if etype == "overloaded_error" or status == 529:
        return ProviderError(ErrorKind.UNAVAILABLE, NAME, "overloaded", status)
    if etype == "rate_limit_error":
        return ProviderError(ErrorKind.RATE_LIMIT, NAME, "rate limited", status)
    if etype == "not_found_error" or status == 404:
        return ProviderError(ErrorKind.NOT_CONFIGURED, NAME, "model not found", status)
    if etype in ("authentication_error", "permission_error"):
        return ProviderError(ErrorKind.AUTH, NAME, "authentication failed", status)
    return generic_status(NAME, status)


class AnthropicProvider:
    name = NAME

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.Client | None = None):
        self._key = api_key
        self._url = base_url.rstrip("/") + "/messages"
        self._client = client or httpx.Client()

    def complete(self, request: CompletionRequest, model: str) -> RawCompletion:
        system = request.system or ""
        if request.json_mode:
            system = (system + "\n\n" if system else "") + \
                "Respond with a single valid JSON object and nothing else."
        body: dict = {"model": model, "max_tokens": request.max_tokens,
                      "messages": _merge_turns(request)}
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = min(request.temperature, 1.0)  # Anthropic range is 0..1
        payload = post_json(self._client, NAME, self._url,
                            headers={"x-api-key": self._key, "anthropic-version": API_VERSION},
                            body=body, timeout=request.timeout_seconds, classify=classify)
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(ErrorKind.UNAVAILABLE, NAME, "malformed response")
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        stop = payload.get("stop_reason") or "end_turn"
        if stop == "refusal" and not text:
            raise ProviderError(ErrorKind.REFUSED, NAME, "refusal")
        usage = payload.get("usage") or {}
        return RawCompletion(text=text, finish_reason=_finish(stop),
                             usage=Usage(input_tokens=int(usage.get("input_tokens") or 0),
                                         output_tokens=int(usage.get("output_tokens") or 0)))


def _merge_turns(request: CompletionRequest) -> list[dict]:
    """Anthropic needs alternating roles starting with user; merge consecutive same-role turns."""
    merged: list[dict] = []
    for m in request.messages:
        if merged and merged[-1]["role"] == m.role:
            merged[-1]["content"] += "\n\n" + m.content
        else:
            merged.append({"role": m.role, "content": m.content})
    if merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(conversation continues)"})
    return merged


def _finish(reason: str) -> str:
    return {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "max_tokens",
            "refusal": "refused"}.get(reason, reason)
