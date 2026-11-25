"""HTTP endpoint auth/behaviour and the app client, end to end in-process."""
import httpx
import pytest
from fastapi.testclient import TestClient

from aj_llm_layer.api import app
from aj_llm_layer.client import LLMClient
from aj_llm_layer.execution import GatewayAdapter, Request
from aj_llm_layer.gateway import ErrorKind, Gateway, GatewayError, load_config
from aj_llm_layer.gateway import http as gateway_http

from .test_router import BASE_ENV, Fake

AJ_KEY = "a" * 40
CALIBRE_KEY = "c" * 40


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("AJ_LLM_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_GATEWAY_KEYS", f"aj_assistant:{AJ_KEY},calibre:{CALIBRE_KEY}")
    fakes = {"openai": Fake("openai", [ErrorKind.QUOTA]), "anthropic": Fake("anthropic", [])}
    gateway_http.set_gateway(Gateway(load_config(BASE_ENV), providers=fakes, retry_delay=0))
    yield TestClient(app), fakes
    gateway_http.set_gateway(None)


BODY = {"messages": [{"role": "user", "content": "hi"}], "data_class": "non_personal"}


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_disabled_by_default_returns_404(monkeypatch):
    monkeypatch.delenv("AJ_LLM_GATEWAY_ENABLED", raising=False)
    assert TestClient(app).post("/v1/complete", json=BODY).status_code == 404
    assert TestClient(app).get("/v1/status").status_code == 404


def test_enabled_without_keys_fails_closed(monkeypatch):
    monkeypatch.setenv("AJ_LLM_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_GATEWAY_KEYS", "aj:short")
    assert TestClient(app).post("/v1/complete", json=BODY, headers=auth("short")).status_code == 503


def test_rejects_missing_and_wrong_keys(setup):
    client, _ = setup
    assert client.post("/v1/complete", json=BODY).status_code == 401
    assert client.post("/v1/complete", json=BODY, headers=auth("x" * 40)).status_code == 401


def test_complete_with_fallback(setup, caplog):
    client, _ = setup
    with caplog.at_level("INFO", logger="aj.llm_layer.gateway"):
        r = client.post("/v1/complete", json=BODY, headers=auth(CALIBRE_KEY))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["provider"] == "anthropic" and data["fallback_used"] is True
    assert "app=calibre" in caplog.text  # the app comes from the key, not the body


def test_validation_errors_do_not_echo_input(setup):
    client, _ = setup
    r = client.post("/v1/complete", json={"messages": [{"role": "robot", "content": "SECRET"}]},
                    headers=auth(AJ_KEY))
    assert r.status_code == 422 and "SECRET" not in r.text


def test_gateway_failure_maps_to_status(setup):
    client, _ = setup
    r = client.post("/v1/complete", json={"messages": [{"role": "user", "content": "hi"}]},
                    headers=auth(AJ_KEY))  # personal data, openai out of credits, anthropic not approved
    assert r.status_code == 503 and r.json()["error"] == "quota"


def test_status_endpoint(setup):
    client, _ = setup
    r = client.get("/v1/status", headers=auth(AJ_KEY))
    assert r.status_code == 200 and "anthropic" in r.json()["providers"]


def test_python_client_end_to_end(setup):
    client, _ = setup
    llm = LLMClient("http://testserver", AJ_KEY, client=client)
    reply = llm.chat("hello", system="be nice", data_class="non_personal", purpose="email_summary")
    assert reply.text == "from anthropic" and reply.fallback_used
    with pytest.raises(GatewayError) as exc:
        llm.chat("hello")  # personal → no approved provider has credit
    assert exc.value.kind is ErrorKind.QUOTA and exc.value.attempts


def test_python_client_unreachable():
    def down(request):
        raise httpx.ConnectError("down", request=request)
    llm = LLMClient("http://nowhere", AJ_KEY, client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(GatewayError) as exc:
        llm.chat("hi")
    assert exc.value.kind is ErrorKind.UNAVAILABLE


def test_routing_execution_adapter_pins_provider_and_model():
    fakes = {"openai": Fake("openai", []), "anthropic": Fake("anthropic", [])}
    gw = Gateway(load_config(BASE_ENV), providers=fakes, retry_delay=0)
    adapter = GatewayAdapter(gw, provider="anthropic", data_class="non_personal")
    result = adapter.complete(Request(model="claude-routed", task_family="cortex_analysis", prompt="x"))
    assert result.model == "claude-routed" and fakes["anthropic"].calls == ["claude-routed"]
    assert fakes["openai"].calls == []
