"""Provider-agnostic selection: evidence and price decide, credit problems route around."""
import random

import pytest
from fastapi.testclient import TestClient

from aj_llm_layer.api import app
from aj_llm_layer.gateway import (CompletionRequest, ErrorKind, Gateway, GatewayError, Message,
                                  SelectorSettings, load_catalog, load_config)
from aj_llm_layer.gateway import http as gateway_http
from aj_llm_layer.gateway.providers import RawCompletion
from aj_llm_layer.gateway.types import Usage
from aj_llm_layer.gateway.errors import ProviderError

from .test_router import Clock

ENV = {"OPENAI_API_KEY": "sk-o", "ANTHROPIC_API_KEY": "sk-a"}  # routed is the default


class ModelFake:
    """Per-model scripted provider: script maps model id -> list of outcomes."""

    def __init__(self, name, script=None, text="answer"):
        self.name = name
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.text = text

    def complete(self, request, model):
        self.calls.append(model)
        steps = self.script.get(model, [])
        step = steps.pop(0) if steps else "ok"
        if step != "ok":
            raise ProviderError(step, self.name, "scripted")
        return RawCompletion(text=self.text, finish_reason="stop",
                             usage=Usage(input_tokens=1000, output_tokens=500))


class Rng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def build(env_extra=None, openai=None, anthropic=None, explore=0.0, text="answer", clock=None, **settings):
    clock = clock or Clock()
    clock.now = 1_790_000_000
    fakes = {"openai": ModelFake("openai", openai, text), "anthropic": ModelFake("anthropic", anthropic, text)}
    gw = Gateway(load_config(dict(ENV, **(env_extra or {}))), providers=fakes, retry_delay=0,
                 clock=clock, wall_clock=clock, rng=Rng(0.0 if explore else 1.0),
                 settings=SelectorSettings(explore_rate=explore, **settings))
    return gw, fakes, clock


def req(**kw):
    kw.setdefault("messages", (Message(role="user", content="hello"),))
    kw.setdefault("data_class", "non_personal")
    kw.setdefault("task_family", "conversation")
    return CompletionRequest(**kw)


def grade(gw, family, model, correct, wrong=0):
    for _ in range(correct):
        gw.ledger.grade(family, model, True)
    for _ in range(wrong):
        gw.ledger.grade(family, model, False)


def test_no_provider_is_preferred_cold_start_goes_cheapest_first():
    gw, fakes, _ = build()
    out = gw.complete(req(tier="fast"))
    # Haiku 5.5 ($0.10/$0.50) is cheaper than gpt-6.1-terra ($0.50/$2.50): price decides.
    assert out.model == "claude-haiku-5-5" and out.selection == "untested"
    order = [c.model for c in gw.selector.shortlist(req(tier="fast"))[0]]
    assert order[:2] == ["claude-haiku-5-5", "gpt-6.1-terra"]
    assert "claude-sonnet-5-5" in order and order.index("claude-sonnet-5-5") > 1  # stronger tier only as backup


def test_proven_model_beats_cheaper_untested_one():
    gw, _, _ = build()
    grade(gw, "conversation", "gpt-6.1-terra", 40)
    out = gw.complete(req(tier="fast"))
    assert out.model == "gpt-6.1-terra" and out.selection == "proven"


def test_poor_quality_model_is_demoted_even_if_cheapest():
    gw, _, _ = build()
    grade(gw, "conversation", "claude-haiku-5-5", 10, wrong=30)   # 25% correct
    candidates = gw.selector.shortlist(req(tier="fast"))[0]
    assert [(c.model, c.selection) for c in candidates[:2]] == [
        ("gpt-6.1-terra", "untested"), ("claude-haiku-5-5", "proven_poor")]


def test_cost_efficient_picks_cheapest_per_correct_answer_and_quality_first_picks_best():
    gw, _, _ = build()
    for _ in range(200):  # record real costs so cost-per-correct is measured
        gw.ledger.record_attempt("coding", "claude-haiku-5-5", ok=True, timeout=False, latency_ms=300, cost_usd=0.0004)
        gw.ledger.record_attempt("coding", "gpt-6.1-terra", ok=True, timeout=False, latency_ms=300, cost_usd=0.0018)
    grade(gw, "coding", "claude-haiku-5-5", 180, wrong=20)  # 90% (lower bound ~85%, clears the 80% bar)
    grade(gw, "coding", "gpt-6.1-terra", 198, wrong=2)      # 99%
    cheap = gw.selector.shortlist(req(tier="fast", task_family="coding"))[0]
    best = gw.selector.shortlist(req(tier="fast", task_family="coding", strategy="quality_first"))[0]
    assert cheap[0].model == "claude-haiku-5-5" and cheap[0].selection == "proven"
    assert best[0].model == "gpt-6.1-terra"


def test_thin_evidence_does_not_clear_the_quality_bar():
    gw, _, _ = build()
    grade(gw, "coding", "claude-haiku-5-5", 34, wrong=6)  # 85% but only 40 samples: lower bound ~71%
    shortlist = gw.selector.shortlist(req(tier="fast", task_family="coding"))[0]
    assert shortlist[-1].model != "claude-haiku-5-5"
    assert ("claude-haiku-5-5", "proven_poor") in [(c.model, c.selection) for c in shortlist]


def test_evidence_is_per_task_family():
    gw, _, _ = build()
    grade(gw, "coding", "gpt-6.1-terra", 40)
    assert gw.complete(req(tier="fast", task_family="coding")).model == "gpt-6.1-terra"
    assert gw.complete(req(tier="fast", task_family="translation")).model == "claude-haiku-5-5"


def test_out_of_credit_provider_is_skipped_for_all_its_models():
    gw, fakes, _ = build(anthropic={"claude-haiku-5-5": [ErrorKind.QUOTA]})
    out = gw.complete(req(tier="fast"))
    assert out.model == "gpt-6.1-terra" and out.fallback_used
    # Next call doesn't even try Anthropic, and its stronger-tier models are skipped too.
    shortlist, skipped, _ = gw.selector.shortlist(req(tier="fast"))
    assert all(c.provider == "openai" for c in shortlist)
    assert {a.model for a in skipped} >= {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
    assert fakes["anthropic"].calls == ["claude-haiku-5-5"]


def test_credit_failure_mid_call_skips_that_providers_other_models():
    gw, fakes, _ = build(anthropic={"claude-haiku-5-5": [ErrorKind.QUOTA]},
                         openai={"gpt-6.1-terra": [ErrorKind.UNAVAILABLE, ErrorKind.UNAVAILABLE]})
    out = gw.complete(req(tier="fast"))
    # haiku: out of credit → terra: down twice → stronger tier: sonnet skipped (no credit) → gpt-6.2-sol
    assert out.model == "gpt-6.2-sol" and out.selection == "stronger_tier"
    assert "claude-sonnet-5-5" not in fakes["anthropic"].calls


def test_personal_data_only_considers_approved_providers():
    gw, fakes, _ = build()
    out = gw.complete(req(tier="fast", data_class="personal"))
    assert out.provider == "openai"
    gw2, _, _ = build({"AJ_LLM_PERSONAL_DATA_PROVIDERS": "openai,anthropic"})
    assert gw2.complete(req(tier="fast", data_class="personal")).provider == "anthropic"


def test_unknown_model_falls_back_without_benching_provider():
    gw, _, _ = build(anthropic={"claude-haiku-5-5": [ErrorKind.NOT_CONFIGURED]})
    out = gw.complete(req(tier="fast"))
    assert out.model == "gpt-6.1-terra"
    assert gw.health.blocked("anthropic") is None


def test_exploration_sends_some_traffic_to_untested_models():
    gw, _, _ = build(explore=1.0)
    grade(gw, "conversation", "gpt-6.1-terra", 40)
    out = gw.complete(req(tier="fast"))
    assert out.model == "claude-haiku-5-5" and out.selection == "explore"


def test_context_limit_gate():
    gw, _, _ = build()
    big = "x" * (4 * 250_000)  # ~250k tokens: beyond haiku (200k) and terra (200k)
    out = gw.complete(req(tier="fast", messages=(Message(role="user", content="a"),), max_tokens=10))
    assert out.model == "claude-haiku-5-5"
    shortlist, skipped, _ = gw.selector.shortlist(CompletionRequest(
        messages=(Message(role="user", content=big[:399_000]),), data_class="non_personal", tier="fast"))
    assert {a.model for a in skipped if a.outcome == "skipped:context_limit"} == set()  # 99k tokens fits
    assert shortlist[0].model == "claude-haiku-5-5"


def test_invalid_json_is_graded_wrong_automatically():
    gw, _, _ = build(text="not json")
    out = gw.complete(req(tier="fast", json_mode=True))
    summary = gw.ledger.summary("conversation", out.model)
    assert summary["graded"] == 1 and summary["correct"] == 0


def test_high_stakes_family_never_auto_promotes():
    gw, _, _ = build()
    grade(gw, "financial_analysis", "gpt-6.1-terra", 100)
    first = gw.selector.shortlist(req(tier="fast", task_family="financial_analysis"))[0][0]
    assert first.selection == "untested"  # engine keeps high-stakes off evidence-based routing


def test_unknown_task_family_rejected():
    with pytest.raises(ValueError):
        req(task_family="astrology")


def test_catalogue_prices_and_long_context_surcharge():
    cat = load_catalog({})
    haiku = cat.get("claude-haiku-5-5")
    assert haiku.cost(1_000_000, 0) == pytest.approx(0.50)   # >100k prompt: $0.50/M
    assert haiku.cost(100_000, 1_000) == pytest.approx(0.0105)  # 100k*$0.10/M + 1k*$0.50/M
    assert load_catalog({"AJ_LLM_DISABLED_MODELS": "gpt-6.2-astra"}).get("gpt-6.2-astra") is None


def test_cost_from_catalogue_feeds_spend_limits():
    gw, _, _ = build({"AJ_LLM_SPEND_LIMITS": '{"anthropic": 0.0005}'})
    first = gw.complete(req(tier="fast"))   # haiku: 1000*0.1/M + 500*0.5/M = 0.00035
    second = gw.complete(req(tier="fast"))  # total 0.0007 → over limit after this
    third = gw.complete(req(tier="fast"))
    assert (first.model, second.model, third.model) == ("claude-haiku-5-5", "claude-haiku-5-5", "gpt-6.1-terra")
    assert first.cost_usd == pytest.approx(0.00035)


# ---- HTTP: feedback loop and leaderboard -------------------------------------------

KEY_A, KEY_B = "a" * 40, "b" * 40


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AJ_LLM_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_GATEWAY_KEYS", f"aj_assistant:{KEY_A},calibre:{KEY_B}")
    gw, _, _ = build()
    gateway_http.set_gateway(gw)
    yield TestClient(app), gw
    gateway_http.set_gateway(None)


def H(key):
    return {"Authorization": f"Bearer {key}"}


def test_feedback_teaches_the_leaderboard(client):
    c, gw = client
    body = {"messages": [{"role": "user", "content": "hi"}], "data_class": "non_personal",
            "tier": "fast", "task_family": "writing"}
    out = c.post("/v1/complete", json=body, headers=H(KEY_A)).json()
    assert out["call_id"].startswith("call_") and out["selection"] == "untested"
    r = c.post("/v1/feedback", json={"call_id": out["call_id"], "correct": True}, headers=H(KEY_A))
    assert r.status_code == 200 and r.json()["model"] == out["model"]
    assert c.post("/v1/feedback", json={"call_id": out["call_id"], "correct": True},
                  headers=H(KEY_A)).status_code == 409   # once only
    board = c.get("/v1/models?task_family=writing", headers=H(KEY_A)).json()
    top = board["families"]["writing"][0]
    assert top["model"] == out["model"] and top["graded"] == 1 and top["correct"] == 1


def test_feedback_only_from_the_app_that_made_the_call(client):
    c, _ = client
    out = c.post("/v1/complete", json={"messages": [{"role": "user", "content": "hi"}],
                                       "data_class": "non_personal"}, headers=H(KEY_A)).json()
    assert c.post("/v1/feedback", json={"call_id": out["call_id"], "correct": False},
                  headers=H(KEY_B)).status_code == 403
    assert c.post("/v1/feedback", json={"call_id": "call_" + "0" * 24, "correct": False},
                  headers=H(KEY_A)).status_code == 404
    assert c.post("/v1/feedback", json={"call_id": out["call_id"], "correct": "yes"},
                  headers=H(KEY_A)).status_code == 422


def test_leaderboard_needs_auth(client):
    c, _ = client
    assert c.get("/v1/models").status_code == 401
