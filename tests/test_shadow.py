from fastapi.testclient import TestClient
import pytest
from aj_llm_layer.api import app

client = TestClient(app)
URL = "/internal/v1/shadow"
PAYLOAD = {"task_family": "light_text", "estimated_input_tokens": 100, "estimated_output_tokens": 50}


def test_disabled_without_config(monkeypatch):
    monkeypatch.delenv("AJ_LLM_LAYER_SHADOW_TOKEN", raising=False)
    assert client.post(URL, json=PAYLOAD).status_code == 503


def test_rejects_unauthorized(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "a" * 48)
    assert client.post(URL, json=PAYLOAD).status_code == 401


@pytest.mark.parametrize("mode", ["static", "both"])
def test_rejects_non_ascii_authorization(monkeypatch, mode):
    monkeypatch.setenv("AJ_LLM_LAYER_AUTH_MODE", mode)
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "a" * 48)
    response = client.post(
        URL, headers={b"Authorization": b"Bearer \xff"}, json=PAYLOAD,
    )
    assert response.status_code == 401


def test_non_ascii_static_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_AUTH_MODE", "static")
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "\u00e9" * 48)
    assert client.post(URL, json=PAYLOAD).status_code == 503


def test_rejects_prompt_or_user_data(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "a" * 48)
    r = client.post(URL, headers={"Authorization": "Bearer " + "a" * 48}, json={**PAYLOAD, "prompt": "private"})
    assert r.status_code == 422


def test_shadow_never_executes(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "a" * 48)
    r = client.post(URL, headers={"Authorization": "Bearer " + "a" * 48}, json=PAYLOAD)
    assert r.status_code == 200
    assert r.json()["eligible_providers"] == []
    assert r.json()["billable_execution"] is False
