"""Provider adapters against recorded-shape fake HTTP responses (no network)."""
import json

import httpx
import pytest

from aj_llm_layer.gateway import CompletionRequest, ErrorKind, Message, ProviderError
from aj_llm_layer.gateway.providers import AnthropicProvider, OpenAIProvider


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def req(**kw):
    kw.setdefault("messages", (Message(role="user", content="hello"),))
    return CompletionRequest(**kw)


# ---- OpenAI ----------------------------------------------------------------

def test_openai_success_and_wire_format():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    out = OpenAIProvider("sk-test", client=client(handler)).complete(
        req(system="be brief", temperature=0.2, json_mode=True, max_tokens=50), "gpt-x")
    assert out.text == "hi there" and out.finish_reason == "stop"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (12, 3)
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    assert body["model"] == "gpt-x" and body["max_completion_tokens"] == 50
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0.2


def test_openai_omits_temperature_when_unset():
    def handler(request):
        assert "temperature" not in json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]})
    out = OpenAIProvider("k", client=client(handler)).complete(req(), "m")
    assert out.finish_reason == "max_tokens"


@pytest.mark.parametrize("status,payload,kind", [
    (429, {"error": {"code": "insufficient_quota", "type": "insufficient_quota"}}, ErrorKind.QUOTA),
    (429, {"error": {"code": "rate_limit_exceeded"}}, ErrorKind.RATE_LIMIT),
    (401, {"error": {"code": "invalid_api_key"}}, ErrorKind.AUTH),
    (500, {}, ErrorKind.UNAVAILABLE),
    (503, {}, ErrorKind.UNAVAILABLE),
    (400, {"error": {"code": "context_length_exceeded"}}, ErrorKind.BAD_REQUEST),
    (400, {"error": {"code": "content_policy_violation"}}, ErrorKind.REFUSED),
])
def test_openai_error_classification(status, payload, kind):
    provider = OpenAIProvider("k", client=client(lambda r: httpx.Response(status, json=payload)))
    with pytest.raises(ProviderError) as exc:
        provider.complete(req(), "m")
    assert exc.value.kind is kind


def test_openai_timeout_and_network():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("down", request=request)
    for handler, kind in ((timeout, ErrorKind.TIMEOUT), (down, ErrorKind.UNAVAILABLE)):
        with pytest.raises(ProviderError) as exc:
            OpenAIProvider("k", client=client(handler)).complete(req(), "m")
        assert exc.value.kind is kind


def test_openai_malformed_success_is_unavailable():
    provider = OpenAIProvider("k", client=client(lambda r: httpx.Response(200, json={"nope": 1})))
    with pytest.raises(ProviderError) as exc:
        provider.complete(req(), "m")
    assert exc.value.kind is ErrorKind.UNAVAILABLE


# ---- Anthropic -------------------------------------------------------------

def test_anthropic_success_and_wire_format():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "AJ"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 9, "output_tokens": 2}})

    messages = (Message(role="user", content="a"), Message(role="user", content="b"),
                Message(role="assistant", content="c"), Message(role="user", content="d"))
    out = AnthropicProvider("ak", client=client(handler)).complete(
        req(messages=messages, system="sys", temperature=1.7, max_tokens=99), "claude-x")
    assert out.text == "hello AJ" and out.finish_reason == "stop"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (9, 2)
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "ak"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    body = seen["body"]
    assert body["system"] == "sys" and body["max_tokens"] == 99 and body["model"] == "claude-x"
    assert body["temperature"] == 1.0  # clamped to Anthropic's range
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][0]["content"] == "a\n\nb"


def test_anthropic_json_mode_adds_instruction_and_conversation_must_start_with_user():
    def handler(request):
        body = json.loads(request.content)
        assert "valid JSON" in body["system"]
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}], "stop_reason": "max_tokens"})
    messages = (Message(role="assistant", content="earlier"), Message(role="user", content="now"))
    out = AnthropicProvider("k", client=client(handler)).complete(req(messages=messages, json_mode=True), "m")
    assert out.finish_reason == "max_tokens"


@pytest.mark.parametrize("status,payload,kind", [
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Your credit balance is too low to access the Anthropic API."}},
     ErrorKind.QUOTA),
    (402, {"error": {"type": "billing_error", "message": "x"}}, ErrorKind.QUOTA),
    (429, {"error": {"type": "rate_limit_error"}}, ErrorKind.RATE_LIMIT),
    (529, {"error": {"type": "overloaded_error"}}, ErrorKind.UNAVAILABLE),
    (401, {"error": {"type": "authentication_error"}}, ErrorKind.AUTH),
    (400, {"error": {"type": "invalid_request_error", "message": "bad field"}}, ErrorKind.BAD_REQUEST),
])
def test_anthropic_error_classification(status, payload, kind):
    provider = AnthropicProvider("k", client=client(lambda r: httpx.Response(status, json=payload)))
    with pytest.raises(ProviderError) as exc:
        provider.complete(req(), "m")
    assert exc.value.kind is kind


def test_errors_never_carry_provider_text():
    payload = {"error": {"type": "invalid_request_error", "message": "SECRET prompt echo"}}
    provider = AnthropicProvider("k", client=client(lambda r: httpx.Response(400, json=payload)))
    with pytest.raises(ProviderError) as exc:
        provider.complete(req(), "m")
    assert "SECRET" not in str(exc.value)
