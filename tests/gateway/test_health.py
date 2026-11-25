"""Credit awareness: escalating benching, spend limits, shared state, owner reset."""

import pytest
from fastapi.testclient import TestClient

from aj_llm_layer.api import app
from aj_llm_layer.gateway import ErrorKind, Gateway, GatewayError, load_config
from aj_llm_layer.gateway import http as gateway_http
from aj_llm_layer.gateway.health import MemoryStore, ProviderHealth, RedisStore

from .test_router import BASE_ENV, Clock, Fake, make, req


def test_quota_bench_escalates_and_success_clears_it():
    clock = Clock()
    health = ProviderHealth(MemoryStore(), clock=clock)
    health.record_failure("openai", ErrorKind.QUOTA)
    assert health.blocked("openai") == "cooling_down:quota"
    clock.now += 601
    assert health.blocked("openai") is None  # one probe allowed
    health.record_failure("openai", ErrorKind.QUOTA)  # still empty → 30 min
    clock.now += 1000
    assert health.blocked("openai") == "cooling_down:quota"
    clock.now += 801
    health.record_failure("openai", ErrorKind.QUOTA)  # → 2 h
    assert health.status("openai")["benched"]["strikes"] == 3
    assert health.status("openai")["benched"]["seconds_left"] == 7200
    health.record_failure("openai", ErrorKind.QUOTA)
    health.record_failure("openai", ErrorKind.QUOTA)  # capped at 6 h
    assert health.status("openai")["benched"]["seconds_left"] == 21600
    health.record_success("openai", None)
    assert health.blocked("openai") is None and health.status("openai")["benched"] is None


def test_rate_limit_is_short_and_does_not_escalate():
    clock = Clock()
    health = ProviderHealth(MemoryStore(), clock=clock)
    for _ in range(3):
        health.record_failure("anthropic", ErrorKind.RATE_LIMIT)
    assert health.status("anthropic")["benched"]["seconds_left"] == 20


def test_outages_do_not_bench():
    health = ProviderHealth(MemoryStore(), clock=Clock())
    health.record_failure("openai", ErrorKind.UNAVAILABLE)
    health.record_failure("openai", ErrorKind.TIMEOUT)
    assert health.blocked("openai") is None


def test_spend_limit_skips_provider_before_credits_run_out():
    env = {"AJ_LLM_PRICES": '{"gpt-test": [2.0, 8.0]}', "AJ_LLM_SPEND_LIMITS": '{"openai": 0.01}'}
    gw, fakes = make(env)
    first = gw.complete(req(data_class="non_personal"))   # costs 0.006
    second = gw.complete(req(data_class="non_personal"))  # total 0.012 → limit reached
    assert first.provider == second.provider == "openai"
    third = gw.complete(req(data_class="non_personal"))
    assert third.provider == "anthropic" and third.attempts[0].outcome == "skipped:spend_limit_reached"
    assert len(fakes["openai"].calls) == 2
    assert gw.status()["openai"]["spend_limit_usd"] == 0.01


def test_spend_limit_resets_next_month():
    clock = Clock()
    clock.now = 1_767_225_600 - 10  # 2025-12-31 23:59:50 UTC
    env = {"AJ_LLM_PRICES": '{"gpt-test": [2.0, 8.0]}', "AJ_LLM_SPEND_LIMITS": '{"openai": 0.001}'}
    gw, _ = make(env, clock=clock)
    gw.complete(req(data_class="non_personal"))
    assert gw.complete(req(data_class="non_personal")).provider == "anthropic"
    clock.now += 20  # new month
    assert gw.complete(req(data_class="non_personal")).provider == "openai"


def test_only_provider_over_limit_reports_quota():
    env = {"AJ_LLM_PRICES": '{"gpt-test": [2.0, 8.0]}', "AJ_LLM_SPEND_LIMITS": '{"openai": 0}'}
    gw, _ = make(env)
    with pytest.raises(GatewayError) as exc:
        gw.complete(req())  # personal → openai only, and it is at its limit
    assert exc.value.kind is ErrorKind.QUOTA


def test_redis_store_shares_state_between_gateways():
    from .fakeredis_stub import FakeRedis
    shared = FakeRedis()
    clock = Clock()
    fakes_a = {"openai": Fake("openai", [ErrorKind.QUOTA]), "anthropic": Fake("anthropic", [])}
    fakes_b = {"openai": Fake("openai", []), "anthropic": Fake("anthropic", [])}
    a = Gateway(load_config(BASE_ENV), providers=fakes_a, retry_delay=0, clock=clock,
                health=ProviderHealth(RedisStore(shared), clock=clock))
    b = Gateway(load_config(BASE_ENV), providers=fakes_b, retry_delay=0, clock=clock,
                health=ProviderHealth(RedisStore(shared), clock=clock))
    a.complete(req(data_class="non_personal"))          # replica A learns OpenAI is empty
    out = b.complete(req(data_class="non_personal"))    # replica B skips it without trying
    assert out.provider == "anthropic" and fakes_b["openai"].calls == []


def test_redis_outage_falls_back_to_memory():
    class Broken:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise ConnectionError("down")
            return fail
    clock = Clock()
    health = ProviderHealth(RedisStore(Broken()), clock=clock)
    health.record_failure("openai", ErrorKind.QUOTA)
    assert health.blocked("openai") == "cooling_down:quota"
    health.record_success("anthropic", 0.5)
    assert health.status("anthropic")["spend_this_month_usd"] == 0.5


ADMIN = "z" * 40


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("AJ_LLM_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_GATEWAY_KEYS", "aj_assistant:" + "a" * 40)
    monkeypatch.setenv("AJ_LLM_ADMIN_KEY", ADMIN)
    gw, _ = make(openai=[ErrorKind.QUOTA])
    gw.complete(req(data_class="non_personal"))  # bench openai
    gateway_http.set_gateway(gw)
    yield TestClient(app), gw
    gateway_http.set_gateway(None)


def test_owner_reset_after_top_up(admin_client):
    client, gw = admin_client
    assert gw.health.blocked("openai") == "cooling_down:quota"
    assert client.post("/v1/admin/providers/openai/reset").status_code == 401
    # an app key is not an admin key
    assert client.post("/v1/admin/providers/openai/reset",
                       headers={"Authorization": "Bearer " + "a" * 40}).status_code == 401
    r = client.post("/v1/admin/providers/openai/reset", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200 and r.json()["available"] is True
    assert client.post("/v1/admin/providers/nope/reset",
                       headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 404


def test_reset_needs_admin_key_configured(admin_client, monkeypatch):
    client, _ = admin_client
    monkeypatch.delenv("AJ_LLM_ADMIN_KEY")
    assert client.post("/v1/admin/providers/openai/reset",
                       headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 503
