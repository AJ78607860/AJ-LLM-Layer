"""Thin client for apps that call the LLM Layer over HTTP (AJ Assistant, Calibre, ...).

    from aj_llm_layer.client import LLMClient
    llm = LLMClient(base_url=os.environ["AJ_LLM_LAYER_URL"], api_key=os.environ["AJ_LLM_LAYER_KEY"])
    reply = llm.chat("Summarise this email", system="You are AJ.", purpose="email_summary",
                     data_class="personal")
    print(reply.text, reply.provider, reply.fallback_used)

Only depends on httpx and the shared pydantic types.
"""
from __future__ import annotations

import httpx

from .gateway.errors import ErrorKind, GatewayError
from .gateway.types import Attempt, CompletionRequest, CompletionResult, Message


class LLMClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120.0,
                 client: httpx.Client | None = None):
        self._url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout
        self._client = client or httpx.Client()

    def complete(self, request: CompletionRequest) -> CompletionResult:
        try:
            response = self._client.post(f"{self._url}/v1/complete", headers=self._headers,
                                         content=request.model_dump_json(),
                                         timeout=self._timeout)
        except httpx.TimeoutException:
            raise GatewayError(ErrorKind.TIMEOUT, (), "LLM Layer timed out") from None
        except httpx.HTTPError:
            raise GatewayError(ErrorKind.UNAVAILABLE, (), "LLM Layer unreachable") from None
        if response.status_code == 200:
            return CompletionResult.model_validate(response.json())
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        try:
            kind = ErrorKind(payload.get("error"))
        except ValueError:
            kind = ErrorKind.BAD_REQUEST if response.status_code in (400, 422) else ErrorKind.UNAVAILABLE
        attempts = tuple(Attempt.model_validate(a) for a in payload.get("attempts", []))
        raise GatewayError(kind, attempts,
                           payload.get("message") or f"LLM Layer returned HTTP {response.status_code}")

    def chat(self, prompt: str, *, system: str | None = None, history: list[dict] | None = None,
             **options) -> CompletionResult:
        messages = [Message(**m) for m in (history or [])]
        messages.append(Message(role="user", content=prompt))
        return self.complete(CompletionRequest(messages=tuple(messages), system=system, **options))
