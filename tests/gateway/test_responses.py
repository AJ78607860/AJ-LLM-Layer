"""OpenAI-Responses-compatible endpoint: translation, tool loops, chaining, privacy, credit fallback, errors."""
import json

import pytest
from fastapi.testclient import TestClient

from aj_llm_layer.api import app
from aj_llm_layer.gateway import ErrorKind, Gateway, ProviderError, SelectorSettings, load_config
from aj_llm_layer.gateway import http as gateway_http
from aj_llm_layer.gateway.responses import ResponseStore, ResponsesService, UnsupportedInput, parse_input
from aj_llm_layer.gateway.responses.items import Call, Image, Msg, Result, Text
from aj_llm_layer.gateway.responses.translate import anthropic_output, anthropic_request, can_serve

from .test_router import Clock
from .test_selection import Rng

KEY = "a" * 40
OTHER = "c" * 40


class FakeProvider:
    """Records the exact payload sent upstream; replies in the provider's own wire format."""

    def __init__(self, name, script=None):
        self.name, self.script, self.sent = name, list(script or []), []

    def post(self, path, body, timeout):
        self.sent.append((path, json.loads(json.dumps(body))))
        step = self.script.pop(0) if self.script else "text"
        if isinstance(step, ErrorKind):
            raise ProviderError(step, self.name, "scripted")
        if self.name == "openai":
            if step == "tool":
                out = [{"type": "function_call", "id": "fc_1", "call_id": "call_o1", "name": "get_weather",
                        "arguments": '{"city":"Leeds"}', "status": "completed"}]
            else:
                out = [{"type": "reasoning", "id": "rs_1", "summary": []},
                       {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Sunny in Leeds", "annotations": []}]}]
            return {"id": "resp_upstream", "status": "completed", "output": out,
                    "usage": {"input_tokens": 50, "output_tokens": 10}}
        if step == "tool":
            content = [{"type": "text", "text": "Let me check."},
                       {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Leeds"}}]
            stop = "tool_use"
        else:
            content, stop = [{"type": "text", "text": "Sunny in Leeds"}], "end_turn"
        return {"content": content, "stop_reason": stop, "usage": {"input_tokens": 50, "output_tokens": 10}}


def build(env=None, openai=None, anthropic=None):
    clock = Clock()
    clock.now = 1_790_000_000
    cfg = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k", **(env or {})}
    fakes = {"openai": FakeProvider("openai", openai), "anthropic": FakeProvider("anthropic", anthropic)}
    gw = Gateway(load_config(cfg), providers=fakes, retry_delay=0, clock=clock, wall_clock=clock,
                 rng=Rng(1.0), settings=SelectorSettings(explore_rate=0))
    return gw, fakes


TOOLS = [{"type": "function", "name": "get_weather", "description": "Weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
ALL = {"AJ_LLM_PERSONAL_DATA_PROVIDERS": "openai,anthropic"}


# ---- translation -----------------------------------------------------------------------------------------

def test_parse_input_items():
    convo = parse_input([
        {"role": "developer", "content": "be brief"},
        {"role": "user", "content": [{"type": "input_text", "text": "hi"},
                                     {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}]},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": '{"a":1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "42"},
        {"type": "reasoning", "id": "rs"},
    ])
    kinds = [type(i).__name__ for i in convo.items]
    assert kinds == ["Msg", "Msg", "Call", "Result"]
    assert convo.items[0].role == "system" and isinstance(convo.items[1].parts[1], Image)
    with pytest.raises(UnsupportedInput):
        parse_input([{"type": "file_search_call"}])
    with pytest.raises(UnsupportedInput):
        parse_input([{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}])


def test_anthropic_translation_of_tool_loop():
    convo = parse_input([
        {"role": "user", "content": "weather in Leeds?"},
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"city":"Leeds"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "sunny"},
    ])
    body = anthropic_request("claude-x", "You are AJ.", convo,
                             {"tools": TOOLS + [{"type": "web_search"}], "tool_choice": "required",
                              "max_output_tokens": 300, "temperature": 1.5, "text": {"format": {"type": "json_object"}}})
    assert body["system"].startswith("You are AJ.") and "JSON" in body["system"]
    assert body["max_tokens"] == 300 and body["temperature"] == 1.0 and body["tool_choice"] == {"type": "any"}
    assert body["tools"][0]["input_schema"]["required"] == ["city"]
    assert body["tools"][1]["type"] == "web_search_20250305"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "get_weather",
                                                 "input": {"city": "Leeds"}}
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "c1", "content": "sunny"}


def test_anthropic_reply_becomes_responses_items():
    items, usage, status = anthropic_output({
        "content": [{"type": "text", "text": "Checking"},
                    {"type": "tool_use", "id": "toolu_9", "name": "get_weather", "input": {"city": "York"}}],
        "stop_reason": "tool_use", "usage": {"input_tokens": 7, "output_tokens": 3}})
    assert items[0]["type"] == "message" and items[0]["content"][0]["text"] == "Checking"
    assert items[1]["type"] == "function_call" and items[1]["call_id"] == "toolu_9"
    assert json.loads(items[1]["arguments"]) == {"city": "York"}
    assert usage == {"input_tokens": 7, "output_tokens": 3} and status == "completed"


def test_anthropic_cannot_take_openai_only_tools_or_non_https_images():
    convo = parse_input("hi")
    assert can_serve("anthropic", [{"type": "file_search"}], convo).startswith("tool_unsupported")
    assert can_serve("anthropic", TOOLS + [{"type": "web_search_preview"}], convo) is None
    assert can_serve("openai", [{"type": "file_search"}], convo) is None
    img = parse_input([{"role": "user", "content": [{"type": "input_image", "image_url": "http://x/a.png"}]}])
    assert can_serve("anthropic", [], img) == "image_unsupported"


# ---- service ---------------------------------------------------------------------------------------------

def test_tool_loop_with_previous_response_id_on_anthropic():
    gw, fakes = build(ALL, openai=[ErrorKind.QUOTA], anthropic=["tool", "text"])
    svc = ResponsesService(gw)
    first = svc.respond({"model": "gpt-whatever", "instructions": "You are AJ.", "input": "weather in Leeds?",
                         "tools": TOOLS}, app="aj")
    calls = [i for i in first["output"] if i["type"] == "function_call"]
    assert first["aj_layer"]["provider"] == "anthropic" and calls and calls[0]["call_id"] == "toolu_1"
    second = svc.respond({"model": "gpt-whatever", "instructions": "You are AJ.", "previous_response_id": first["id"],
                          "input": [{"type": "function_call_output", "call_id": "toolu_1", "output": "sunny"}],
                          "tools": TOOLS}, app="aj")
    assert second["output_text"] == "Sunny in Leeds" and second["previous_response_id"] == first["id"]
    path, sent = fakes["anthropic"].sent[-1]
    assert path == "/messages"
    # the Layer replayed the whole turn: question, the model's tool call, then the tool result
    blocks = [b["type"] for m in sent["messages"] for b in m["content"]]
    assert blocks == ["text", "text", "tool_use", "tool_result"]


def test_openai_passthrough_keeps_hosted_tools_and_strips_reasoning():
    gw, fakes = build(ALL)   # both approved, so Anthropic is skipped only because it can't run file_search
    svc = ResponsesService(gw)
    out = svc.respond({"model": "gpt-6.2-sol", "instructions": "You are AJ.", "input": "hello",
                       "tools": TOOLS + [{"type": "file_search", "vector_store_ids": ["vs_1"]}], "store": True},
                      app="aj")
    path, sent = fakes["openai"].sent[-1]
    assert path == "/responses" and sent["store"] is False and sent["model"] == "gpt-6.2-sol"
    assert sent["tools"][1]["type"] == "file_search" and "previous_response_id" not in sent
    assert [i["type"] for i in out["output"]] == ["message"] and out["output_text"] == "Sunny in Leeds"
    assert fakes["anthropic"].sent == []        # anthropic can't carry file_search: skipped, not tried
    assert any(a["outcome"].startswith("skipped:tool_unsupported") for a in out["aj_layer"]["attempts"])


def test_personal_data_follows_approval():
    gw, fakes = build(openai=[ErrorKind.QUOTA])           # default approval: openai only
    svc = ResponsesService(gw)
    from aj_llm_layer.gateway.responses import ResponsesError
    with pytest.raises(ResponsesError) as exc:
        svc.respond({"input": "my bank balance?"}, app="aj")     # header missing -> personal
    body = exc.value.body()
    assert exc.value.status == 429 and body["error"]["code"] == "insufficient_quota"
    assert fakes["anthropic"].sent == []
    # Approved for both: an out-of-credit provider falls through to the other, even for sensitive data.
    gw2, fakes2 = build(ALL, anthropic=[ErrorKind.QUOTA])
    out = ResponsesService(gw2).respond({"input": "my bank balance?"}, app="aj", data_class="sensitive")
    assert out["aj_layer"]["provider"] == "openai" and out["aj_layer"]["fallback_used"]
    assert fakes2["anthropic"].sent and gw2.health.blocked("anthropic") == "cooling_down:quota"


def test_chain_is_private_to_the_app_and_never_downgrades():
    gw, _ = build(ALL)
    svc = ResponsesService(gw)
    first = svc.respond({"input": "hi"}, app="aj", data_class="personal")
    from aj_llm_layer.gateway.responses import ResponsesError
    with pytest.raises(ResponsesError) as exc:
        svc.respond({"previous_response_id": first["id"], "input": "more"}, app="calibre")
    assert exc.value.status == 400 and exc.value.code == "previous_response_not_found"
    assert "previous_response" in str(exc.value)      # AJ's is_chain_error() recognises this and rebuilds
    assert svc.store.get(first["id"], app="aj").data_class == "personal"


def test_expired_chain_and_unsupported_requests():
    gw, _ = build()
    svc = ResponsesService(gw, store=ResponseStore(ttl=0.0))
    first = svc.respond({"input": "hi"}, app="aj", data_class="non_personal")
    from aj_llm_layer.gateway.responses import ResponsesError
    with pytest.raises(ResponsesError):
        svc.respond({"previous_response_id": first["id"], "input": "again"}, app="aj")
    for bad in ({"input": "x", "stream": True}, {"input": []}, {"input": [{"type": "computer_call"}]},
                {"input": "x", "tools": "nope"}):
        with pytest.raises(ResponsesError) as exc:
            svc.respond(bad, app="aj")
        assert exc.value.status == 400


def test_error_mapping_when_everything_fails():
    gw, _ = build(ALL, openai=[ErrorKind.UNAVAILABLE] * 9, anthropic=[ErrorKind.UNAVAILABLE] * 9)
    from aj_llm_layer.gateway.responses import ResponsesError
    with pytest.raises(ResponsesError) as exc:
        ResponsesService(gw).respond({"input": "hi"}, app="aj")
    assert exc.value.status == 503 and exc.value.etype == "server_error"
    gw2, _ = build(ALL, anthropic=[ErrorKind.BAD_REQUEST], openai=[ErrorKind.BAD_REQUEST])
    with pytest.raises(ResponsesError) as exc:
        ResponsesService(gw2).respond({"model": "gpt-6.2-sol", "input": "hi"}, app="aj")
    assert exc.value.status == 400


def test_auto_routing_and_evidence():
    gw, fakes = build(ALL)
    out = ResponsesService(gw).respond({"input": "Prove that the square root of 2 is irrational"}, app="aj")
    assert out["aj_layer"]["task_family"] == "mathematics" and out["aj_layer"]["tier"] == "smart"
    assert out["model"] in ("claude-opus-5-5", "gpt-6.2-astra")
    assert gw.ledger.summary("mathematics", out["model"])["calls"] == 1


def test_equal_prices_are_not_broken_by_name():
    """sonnet and gpt-6.2-sol cost the same; with a real random tie-break both get picked."""
    import random
    clock = Clock()
    clock.now = 1_790_000_000
    seen = set()
    for seed in range(30):
        fakes = {"openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}
        gw = Gateway(load_config({"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}), providers=fakes,
                     retry_delay=0, clock=clock, wall_clock=clock, rng=random.Random(seed),
                     settings=SelectorSettings(explore_rate=0))
        out = ResponsesService(gw).respond({"model": "gpt-6.2-sol", "input": "hello"}, app="aj",
                                           data_class="non_personal")
        seen.add(out["model"])
    assert seen == {"claude-sonnet-5-5", "gpt-6.2-sol"}


# ---- HTTP ------------------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AJ_LLM_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_GATEWAY_KEYS", f"aj_assistant:{KEY},calibre:{OTHER}")
    gw, fakes = build(ALL, anthropic=["tool", "text"], openai=[ErrorKind.QUOTA])
    gateway_http.set_gateway(gw)
    yield TestClient(app), fakes
    gateway_http.set_gateway(None)


def test_http_responses_endpoint(client):
    c, fakes = client
    assert c.post("/v1/responses", json={"input": "hi"}).status_code == 401
    r = c.post("/v1/responses", headers={"Authorization": f"Bearer {KEY}", "X-AJ-Data-Class": "sensitive"},
               json={"instructions": "You are AJ.", "input": "weather?", "tools": TOOLS})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["object"] == "response" and first["output"][-1]["type"] == "function_call"
    r2 = c.post("/v1/responses", headers={"Authorization": f"Bearer {KEY}"},
                json={"previous_response_id": first["id"], "tools": TOOLS,
                      "input": [{"type": "function_call_output", "call_id": "toolu_1", "output": "sunny"}]})
    assert r2.status_code == 200 and r2.json()["output_text"] == "Sunny in Leeds"
    bad = c.post("/v1/responses", headers={"Authorization": f"Bearer {KEY}"}, content=b"{not json")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_json"


def test_time_budget_keeps_fallback_inside_the_callers_wait(monkeypatch):
    from aj_llm_layer.gateway.responses import service as svc_mod
    gw, fakes = build(ALL)
    seen = []

    class Slow(FakeProvider):
        def post(self, path, body, timeout):
            seen.append(timeout)
            raise ProviderError(ErrorKind.TIMEOUT, self.name, "slow")
    gw._providers = {"openai": Slow("openai"), "anthropic": Slow("anthropic")}
    clock = iter(range(0, 10_000, 40))         # every monotonic() call advances 40 s
    monkeypatch.setattr(svc_mod.time, "monotonic", lambda: next(clock))
    from aj_llm_layer.gateway.responses import ResponsesError
    with pytest.raises(ResponsesError) as exc:
        ResponsesService(gw).respond({"input": "hi"}, app="aj")
    assert exc.value.status == 504
    assert all(t <= svc_mod.ATTEMPT_TIMEOUT for t in seen) and len(seen) <= 3
    assert any(a.outcome == "skipped:time_budget" for a in exc.value.attempts)
