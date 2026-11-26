"""OpenAI-Responses-compatible execution: AJ's existing requests, any provider.

`POST /v1/responses` accepts the subset of the OpenAI Responses API that AJ
Assistant uses (instructions, input items, function tools, tool outputs, web
search, JSON output, `previous_response_id`) and runs it through the same
machinery as `/v1/complete`: the question reader, evidence + price selection,
the personal-data gate, credit benching, spend limits and fallback.

Apps keep using the official OpenAI SDK; only `base_url` and the key change.
Errors come back in OpenAI's error shape (e.g. `insufficient_quota`) so an
app's existing error handling keeps working.
"""
from __future__ import annotations

import logging
import time

from ..errors import FALLBACK_KINDS, RETRY_KINDS, ErrorKind, ProviderError
from ..types import Attempt, CompletionRequest, Message, Usage
from .items import Conversation, UnsupportedInput, parse_input
from ..router import _FALLBACK_SKIPS
from .store import ResponseStore
from .translate import (anthropic_output, anthropic_request, can_serve, items_to_conversation, new_id,
                        openai_output, openai_request, wants_json)

log = logging.getLogger("aj.llm_layer.gateway.responses")

# AJ waits at most AJ_OPENAI_TIMEOUT_SECONDS (120 s) for a reply. Each provider attempt gets 45 s and the whole
# request 100 s, so a fallback to the next provider still lands inside the caller's wait.
ATTEMPT_TIMEOUT = 45.0
REQUEST_BUDGET = 100.0

_BUILD = {"openai": (openai_request, "/responses", openai_output),
          "anthropic": (anthropic_request, "/messages", anthropic_output)}

# Failure -> (HTTP status, OpenAI error type, OpenAI error code). Credit problems keep OpenAI's
# `insufficient_quota` code so callers classify them exactly as before.
_ERROR = {
    ErrorKind.QUOTA: (429, "insufficient_quota", "insufficient_quota"),
    ErrorKind.RATE_LIMIT: (429, "rate_limit_error", "rate_limit_exceeded"),
    ErrorKind.TIMEOUT: (504, "server_error", "upstream_timeout"),
    ErrorKind.UNAVAILABLE: (503, "server_error", "upstream_unavailable"),
    ErrorKind.AUTH: (503, "server_error", "upstream_auth"),        # the Layer's provider key, not the caller's
    ErrorKind.NOT_CONFIGURED: (503, "server_error", "no_provider_available"),
    ErrorKind.BAD_REQUEST: (400, "invalid_request_error", "upstream_rejected"),
    ErrorKind.REFUSED: (400, "invalid_request_error", "content_policy_violation"),
}


class ResponsesError(Exception):
    def __init__(self, status: int, message: str, etype: str, code: str | None = None, param: str | None = None,
                 attempts: tuple = ()):
        super().__init__(message)
        self.status, self.etype, self.code, self.param, self.attempts = status, etype, code, param, attempts

    def body(self) -> dict:
        return {"error": {"message": str(self), "type": self.etype, "code": self.code, "param": self.param},
                "aj_layer": {"attempts": [a.model_dump() for a in self.attempts]}}


def _bad(message: str, param: str | None = None, code: str | None = None) -> ResponsesError:
    return ResponsesError(400, message, "invalid_request_error", code, param)


class ResponsesService:
    def __init__(self, gateway, store: ResponseStore | None = None, *, retry_delay: float | None = None):
        self.gw = gateway
        self.store = store if store is not None else ResponseStore()  # an empty store is falsy
        self._retry_delay = gateway._retry_delay if retry_delay is None else retry_delay

    # -- request -> routing view ----------------------------------------------
    def _routing_request(self, body: dict, convo: Conversation, data_class: str) -> CompletionRequest:
        latest = convo.latest_user_text() or "(continue the task)"
        history = convo.text_for_routing()
        messages = [Message(role="user", content=latest[:390_000])]
        if history and history != latest:
            # Older turns count toward size/limits only; the question reader looks at the latest user text.
            messages = [Message(role="user", content=history[:200_000]), Message(role="assistant", content="."),
                        messages[0]]
        hinted = self.gw.catalog.get(str(body.get("model") or ""))
        max_out = body.get("max_output_tokens")
        return CompletionRequest(
            messages=tuple(messages), system=(body.get("instructions") or None) and str(body["instructions"])[:390_000],
            tier=hinted.tier if hinted else "auto", data_class=data_class,
            max_tokens=max(1, min(int(max_out), 64_000)) if isinstance(max_out, int) else 4096,
            json_mode=wants_json(body.get("text")))

    # -- main ------------------------------------------------------------------
    def respond(self, body: dict, *, app: str, data_class: str = "personal") -> dict:
        if not isinstance(body, dict):
            raise _bad("request body must be a JSON object")
        if body.get("stream"):
            raise _bad("streaming is not supported by the AJ LLM Layer yet", "stream", "unsupported_parameter")
        # AJ's classes: "public"/"non_personal" may go anywhere; everything else (personal, sensitive, unknown)
        # is personal data and only goes to providers approved for it.
        data_class = "non_personal" if data_class in ("non_personal", "public") else "personal"

        previous = None
        prev_id = body.get("previous_response_id")
        if prev_id:
            previous = self.store.get(str(prev_id), app=app)
            if previous is None:
                raise _bad("previous_response_id not found or expired; resend the full conversation",
                           "previous_response_id", "previous_response_not_found")
            if previous.data_class == "personal":
                data_class = "personal"      # a conversation never becomes less private
        try:
            convo = Conversation(items=(list(previous.items) if previous else []) + parse_input(body.get("input")).items)
        except UnsupportedInput as exc:
            raise _bad(str(exc), "input") from None
        if not convo.items:
            raise _bad("input is required", "input")
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            raise _bad("tools must be a list", "tools")
        instructions = body.get("instructions")

        request, understanding = self.gw.resolve(self._routing_request(body, convo, data_class))
        candidates, attempts, _ = self.gw._candidates(request)
        if previous is not None:   # keep a tool loop on the model that started it, when it's still usable
            sticky = [c for c in candidates if (c.provider, c.model) == (previous.provider, previous.model)]
            candidates = sticky + [c for c in candidates if c not in sticky]

        started = time.monotonic()
        last: ProviderError | None = None
        for cand in candidates:
            name, model = cand.provider, cand.model
            left = REQUEST_BUDGET - (time.monotonic() - started)
            if left < 5:
                attempts.append(Attempt(provider=name, model=model, outcome="skipped:time_budget"))
                last = last or ProviderError(ErrorKind.TIMEOUT, name, "request time budget used up")
                break
            reason = can_serve(name, tools, convo) or self.gw.health.blocked(name)
            if reason:
                attempts.append(Attempt(provider=name, model=model, outcome=f"skipped:{reason}"))
                continue
            build, path, parse = _BUILD[name]
            payload = build(model, instructions, convo, body)
            for attempt_no in (1, 2):
                t0 = time.monotonic()
                try:
                    timeout = min(ATTEMPT_TIMEOUT, max(5.0, REQUEST_BUDGET - (time.monotonic() - started)))
                    raw = self.gw._providers[name].post(path, payload, timeout=timeout)
                    output, usage, status = parse(raw)
                except ProviderError as exc:
                    ms = int((time.monotonic() - t0) * 1000)
                    attempts.append(Attempt(provider=name, model=model, outcome=exc.kind.value, latency_ms=ms))
                    last = exc
                    self.gw.health.record_failure(name, exc.kind)
                    if exc.kind not in (ErrorKind.BAD_REQUEST, ErrorKind.REFUSED):
                        self.gw.ledger.record_attempt(request.task_family, model, ok=False,
                                                      timeout=exc.kind is ErrorKind.TIMEOUT, latency_ms=ms,
                                                      cost_usd=None)
                    if exc.kind in RETRY_KINDS and attempt_no == 1 and time.monotonic() - started < REQUEST_BUDGET / 2:
                        if self._retry_delay:
                            time.sleep(self._retry_delay)
                        continue
                    break
                ms = int((time.monotonic() - t0) * 1000)
                attempts.append(Attempt(provider=name, model=model, outcome="ok", latency_ms=ms))
                cost = self.gw._cost(model, Usage(**usage))
                self.gw.health.record_success(name, cost)
                self.gw.ledger.record_attempt(request.task_family, model, ok=True, timeout=False,
                                              latency_ms=ms, cost_usd=cost)
                call_id = self.gw.ledger.new_call(family=request.task_family, model=model, provider=name, app=app)
                response_id = new_id("resp")
                self.store.put(response_id, app=app, data_class=data_class,
                               items=convo.items + items_to_conversation(output), provider=name, model=model)
                fallback = any((not a.outcome.startswith("skipped:") or a.outcome.startswith(_FALLBACK_SKIPS))
                               and a.outcome != "ok" for a in attempts[:-1])
                text = "".join(c.get("text") or "" for i in output if i.get("type") == "message"
                               for c in i.get("content") or [] if isinstance(c, dict)
                               and c.get("type") == "output_text")
                log.info("llm_responses app=%s family=%s tier=%s provider=%s model=%s selection=%s latency_ms=%d "
                         "in_tokens=%d out_tokens=%d tool_calls=%d chained=%s fallback=%s attempts=%s",
                         app, request.task_family, request.tier, name, model, cand.selection,
                         int((time.monotonic() - started) * 1000), usage["input_tokens"], usage["output_tokens"],
                         sum(1 for i in output if i.get("type") == "function_call"), previous is not None, fallback,
                         ",".join(f"{a.provider}/{a.model}:{a.outcome}" for a in attempts))
                return {
                    "id": response_id, "object": "response", "created_at": int(time.time()), "status": status,
                    "model": model, "output": output, "output_text": text,
                    "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                              "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}},
                    "instructions": instructions, "tools": tools, "tool_choice": body.get("tool_choice", "auto"),
                    "parallel_tool_calls": bool(body.get("parallel_tool_calls", True)),
                    "previous_response_id": prev_id, "error": None,
                    "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
                    "metadata": body.get("metadata") or {},
                    "aj_layer": {"provider": name, "selection": cand.selection, "call_id": call_id,
                                 "task_family": request.task_family, "tier": request.tier, "cost_usd": cost,
                                 "fallback_used": fallback, "understanding": understanding,
                                 "attempts": [a.model_dump() for a in attempts]},
                }
            if last is not None and last.kind not in FALLBACK_KINDS:
                break
        kind = last.kind if last else self._skip_kind(attempts)
        status, etype, code = _ERROR[kind]
        log.warning("llm_responses app=%s family=%s tier=%s outcome=%s attempts=%s", app, request.task_family,
                    request.tier, kind.value, ",".join(f"{a.provider}/{a.model}:{a.outcome}" for a in attempts))
        message = (f"no model could answer ({kind.value})" if last is None else f"all models failed (last: {last})")
        raise ResponsesError(status, message, etype, code, attempts=tuple(attempts))

    def _skip_kind(self, attempts) -> ErrorKind:
        for a in attempts:
            if a.outcome.startswith("skipped:cooling_down") or a.outcome == "skipped:spend_limit_reached":
                return self.gw.health.bench_reason(a.provider) or ErrorKind.QUOTA
        return ErrorKind.NOT_CONFIGURED
