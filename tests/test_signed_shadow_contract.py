"""Receiver contract tests using dummy keys only; no network or provider calls."""
import json
import time

from fastapi.testclient import TestClient

from aj_llm_layer.api import app
from aj_llm_layer import service_auth
from aj_llm_layer.shadow import correlation_id

KEY = "dummy-receiver-test-key-0123456789-abcdef"
PATH = "/internal/v1/shadow"
PAYLOAD = {"task_family": "light_text", "estimated_input_tokens": 0, "estimated_output_tokens": 0}
BODY = json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()


def setup_signed(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_AUTH_MODE", "signed")
    monkeypatch.setenv("AJ_LLM_LAYER_SIGNING_KEYS", "test:" + KEY)
    monkeypatch.setenv("AJ_ENVIRONMENT", "staging")
    monkeypatch.delenv("AJ_LLM_LAYER_SHADOW_TOKEN", raising=False)


def token(*, now=None, path=PATH, scope="shadow:write", env="staging", body=BODY):
    return service_auth.sign(method="POST", path=path, body=body, scope=scope,
                             key=KEY.encode(), kid="test", env=env, now=now)


def send(auth, body=BODY, correlation="req_1234567890abcdef"):
    return TestClient(app).post(PATH, content=body, headers={
        "Content-Type": "application/json",
        "X-AJ-Service-Auth": auth,
        "X-AJ-Correlation-ID": correlation,
    })


def test_signed_request_is_metadata_only_and_correlated(monkeypatch):
    setup_signed(monkeypatch)
    response = send(token())
    assert response.status_code == 200
    assert response.headers["x-aj-correlation-id"] == "req_1234567890abcdef"
    assert response.json()["mode"] == "shadow_only"
    assert response.json()["eligible_providers"] == []
    assert response.json()["billable_execution"] is False


def test_replay_rejected(monkeypatch):
    setup_signed(monkeypatch)
    credential = token()
    assert send(credential).status_code == 200
    assert send(credential).status_code == 401


def test_expired_credential_rejected(monkeypatch):
    setup_signed(monkeypatch)
    assert send(token(now=time.time() - 120)).status_code == 401


def test_future_credential_rejected(monkeypatch):
    setup_signed(monkeypatch)
    assert send(token(now=time.time() + 120)).status_code == 401


def test_signature_binding_and_tampering(monkeypatch):
    setup_signed(monkeypatch)
    assert send(token(), body=BODY.replace(b"light_text", b"other_text")).status_code == 401
    assert send(token(path="/internal/v1/other")).status_code == 401
    assert send(token(scope="inference:execute")).status_code == 401
    assert send(token(env="production")).status_code == 401


def test_signed_mode_never_downgrades_to_static(monkeypatch):
    setup_signed(monkeypatch)
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "legacy-static-token-" * 3)
    r = TestClient(app).post(PATH, json=PAYLOAD,
        headers={"Authorization": "Bearer " + "legacy-static-token-" * 3})
    assert r.status_code == 401


def test_missing_signing_configuration_fails_closed(monkeypatch):
    setup_signed(monkeypatch)
    monkeypatch.delenv("AJ_LLM_LAYER_SIGNING_KEYS")
    assert send(token()).status_code == 503


def test_invalid_correlation_id_is_replaced():
    cid = correlation_id("req_x\r\nInjected: yes")
    assert cid.startswith("llm_") and len(cid) == 36
    assert correlation_id("req_1234567890abcdef") == "req_1234567890abcdef"


def test_forbidden_content_rejected(monkeypatch):
    setup_signed(monkeypatch)
    bad = dict(PAYLOAD, prompt="private user content")
    body = json.dumps(bad).encode()
    assert send(token(body=body), body=body).status_code == 422


def test_static_compatibility_requires_explicit_mode(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_AUTH_MODE", "static")
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "legacy-static-token-" * 3)
    r = TestClient(app).post(PATH, json=PAYLOAD,
        headers={"Authorization": "Bearer " + "legacy-static-token-" * 3})
    assert r.status_code == 200

def test_aj_sender_golden_vector():
    from pathlib import Path
    v = json.loads((Path(__file__).parent / "fixtures" / "llm_layer_service_auth_vectors.json").read_text())
    credential = service_auth.sign(method=v["method"], path=v["path"], body=v["body"].encode(),
        scope=v["scope"], key=v["key"].encode(), kid=v["key_id"], env=v["env"],
        now=v["now"], jti=v["jti"])
    assert credential == v["header"]
    claims = service_auth.verify(credential, method=v["method"], path=v["path"],
        body=v["body"].encode(), keys={v["key_id"]: v["key"].encode()},
        env=v["env"], scope=v["scope"], replay=service_auth.ReplayCache(), now=v["now"])
    assert claims["jti"] == v["jti"]

def test_unauthenticated_malformed_payload_does_not_reveal_schema(monkeypatch):
    setup_signed(monkeypatch)
    r = TestClient(app).post(PATH, content=b'{"prompt":"private"}',
        headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert r.json() == {"detail": "unauthorized"}


def test_authenticated_malformed_payload_is_rejected(monkeypatch):
    setup_signed(monkeypatch)
    body = b'{"prompt":"private"}'
    r = send(token(body=body), body=body)
    assert r.status_code == 422
    assert r.json() == {"detail": "invalid shadow metadata"}
