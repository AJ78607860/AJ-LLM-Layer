"""OpenAI Chat Completions adapter."""
from __future__ import annotations

import httpx

from ..errors import ErrorKind, ProviderError
from ..types import CompletionRequest, Usage
from .base import RawCompletion, generic_status, post_json

NAME = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
_QUOTA_CODES = {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}


def classify(status: int, payload: dict) -> ProviderError:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or "")
    etype = str(error.get("type") or "")
    if code in _QUOTA_CODES or etype in _QUOTA_CODES:
        return ProviderError(ErrorKind.QUOTA, NAME, "out of credits", status)
    if code == "content_policy_violation" or etype == "content_policy_violation":
        return ProviderError(ErrorKind.REFUSED, NAME, "content policy", status)
    if code == "model_not_found" or status == 404:
        return ProviderError(ErrorKind.NOT_CONFIGURED, NAME, "model not found", status)
    if code == "context_length_exceeded":
        return ProviderError(ErrorKind.BAD_REQUEST, NAME, "context too long", status)
    return generic_status(NAME, status)


class OpenAIProvider:
    name = NAME

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 client: httpx.Client | None = None):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._url = self._base + "/chat/completions"
        self._client = client or httpx.Client()

    def post(self, path: str, body: dict, timeout: float) -> dict:
        """Raw authenticated POST to this provider (used by the Responses translator)."""
        return post_json(self._client, NAME, self._base + path, headers={"Authorization": f"Bearer {self._key}"},
                         body=body, timeout=timeout, classify=classify)

    def complete(self, request: CompletionRequest, model: str) -> RawCompletion:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        body: dict = {"model": model, "messages": messages,
                      "max_completion_tokens": request.max_tokens}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        payload = post_json(self._client, NAME, self._url,
                            headers={"Authorization": f"Bearer {self._key}"},
                            body=body, timeout=request.timeout_seconds, classify=classify)
        try:
            choice = payload["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason") or "stop"
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError(ErrorKind.UNAVAILABLE, NAME, "malformed response") from None
        if finish == "content_filter" and not text:
            raise ProviderError(ErrorKind.REFUSED, NAME, "content filter")
        usage = payload.get("usage") or {}
        return RawCompletion(text=text, finish_reason=_finish(finish),
                             usage=Usage(input_tokens=int(usage.get("prompt_tokens") or 0),
                                         output_tokens=int(usage.get("completion_tokens") or 0)))


def _finish(reason: str) -> str:
    return {"length": "max_tokens", "stop": "stop", "content_filter": "refused"}.get(reason, reason)
