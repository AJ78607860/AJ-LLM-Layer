"""Gateway fallback, privacy gating and circuit breaker with fake providers."""
import logging

import pytest

from aj_llm_layer.gateway import (CompletionRequest, ErrorKind, Gateway, GatewayError, Message,
                                  ProviderError, load_config)
from aj_llm_layer.gateway.providers import RawCompletion
from aj_llm_layer.gateway.types import Usage

BASE_ENV = {"OPENAI_API_KEY": "sk-o", "ANTHROPIC_API_KEY": "sk-a", "AJ_LLM_SELECTION": "ordered",
            "AJ_LLM_OPENAI_MODEL_STANDARD": "gpt-test", "AJ_LLM_OPENAI_MODEL_FAST": "gpt-mini"}


class Fake:
    def __init__(self, name, script):
        self.name = name
        self.script = list(script)  # each item: "ok" or an ErrorKind
        self.calls = []

    def complete(self, request, model):
        self.calls.append(model)
        step = self.script.pop(0) if self.script else "ok"
        if step != "ok":
            raise ProviderError(step, self.name, "scripted")
        return RawCompletion(text=f"from {self.name}", finish_reason="stop",
                             usage=Usage(input_tokens=1000, output_tokens=500))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(env_extra=None, openai=(), anthropic=(), clock=None):
    env = dict(BASE_ENV, **(env_extra or {}))
    fakes = {"openai": Fake("openai", openai), "anthropic": Fake("anthropic", anthropic)}
    clock = clock or Clock()
    gw = Gateway(load_config(env), providers=fakes, retry_delay=0, clock=clock, wall_clock=clock)
    return gw, fakes


def req(**kw):
    kw.setdefault("messages", (Message(role="user", content="hello"),))
    return CompletionRequest(**kw)


def test_primary_success():
    gw, fakes = make()
    out = gw.complete(req(data_class="non_personal"))
    assert out.provider == "openai" and out.model == "gpt-test" and not out.fallback_used
    assert fakes["anthropic"].calls == []


def test_openai_out_of_credits_falls_back_to_anthropic():
    gw, fakes = make(openai=[ErrorKind.QUOTA])
    out = gw.complete(req(data_class="non_personal"))
    assert out.provider == "anthropic" and out.model == "claude-sonnet-5-5" and out.fallback_used
    assert [a.outcome for a in out.attempts] == ["quota", "ok"]


def test_quota_trips_breaker_so_next_call_skips_openai_until_cooldown():
    clock = Clock()
    gw, fakes = make(openai=[ErrorKind.QUOTA], clock=clock)
    gw.complete(req(data_class="non_personal"))
    out = gw.complete(req(data_class="non_personal"))
    assert out.provider == "anthropic"
    assert out.attempts[0].outcome == "skipped:cooling_down:quota"
    assert len(fakes["openai"].calls) == 1
    clock.now += 601
    assert gw.complete(req(data_class="non_personal")).provider == "openai"


def test_personal_data_never_reaches_unapproved_provider():
    gw, fakes = make(openai=[ErrorKind.QUOTA])
    with pytest.raises(GatewayError) as exc:
        gw.complete(req())  # personal by default
    assert exc.value.kind is ErrorKind.QUOTA
    assert fakes["anthropic"].calls == []
    assert "skipped:personal_data_not_approved" in [a.outcome for a in exc.value.attempts]


def test_personal_data_falls_back_once_owner_approves_anthropic():
    gw, _ = make({"AJ_LLM_PERSONAL_DATA_PROVIDERS": "openai,anthropic"}, openai=[ErrorKind.QUOTA])
    assert gw.complete(req()).provider == "anthropic"


def test_transient_error_retries_same_provider_once():
    gw, fakes = make(openai=[ErrorKind.UNAVAILABLE])
    out = gw.complete(req(data_class="non_personal"))
    assert out.provider == "openai" and len(fakes["openai"].calls) == 2


def test_two_transient_errors_then_fallback():
    gw, fakes = make(openai=[ErrorKind.TIMEOUT, ErrorKind.TIMEOUT])
    out = gw.complete(req(data_class="non_personal"))
    assert out.provider == "anthropic"
    assert [a.outcome for a in out.attempts] == ["timeout", "timeout", "ok"]


@pytest.mark.parametrize("kind", [ErrorKind.BAD_REQUEST, ErrorKind.REFUSED])
def test_bad_request_and_refusal_do_not_fall_back(kind):
    gw, fakes = make(openai=[kind])
    with pytest.raises(GatewayError) as exc:
        gw.complete(req(data_class="non_personal"))
    assert exc.value.kind is kind and fakes["anthropic"].calls == []


def test_all_fail_raises_with_trail():
    gw, _ = make(openai=[ErrorKind.QUOTA], anthropic=[ErrorKind.QUOTA])
    with pytest.raises(GatewayError) as exc:
        gw.complete(req(data_class="non_personal"))
    assert [a.provider for a in exc.value.attempts] == ["openai", "anthropic"]


def test_ordered_mode_uses_catalogue_model_when_no_env_override():
    env = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "AJ_LLM_SELECTION": "ordered"}
    fakes = {"openai": Fake("openai", []), "anthropic": Fake("anthropic", [])}
    gw = Gateway(load_config(env), providers=fakes, retry_delay=0)
    out = gw.complete(req(data_class="non_personal", tier="smart"))
    assert out.provider == "openai" and out.model == "gpt-6.2-astra" and out.selection == "ordered"


def test_no_keys_is_not_configured():
    gw = Gateway(load_config({}), retry_delay=0)
    with pytest.raises(GatewayError) as exc:
        gw.complete(req(data_class="non_personal"))
    assert exc.value.kind is ErrorKind.NOT_CONFIGURED


def test_request_can_narrow_and_reorder_providers_and_pin_model():
    gw, fakes = make()
    out = gw.complete(req(data_class="non_personal", providers=("anthropic",), model="claude-custom"))
    assert out.provider == "anthropic" and out.model == "claude-custom"
    assert fakes["openai"].calls == []
    with pytest.raises(ValueError):
        req(model="x")  # pinning a model needs exactly one provider


def test_provider_order_from_env():
    gw, _ = make({"AJ_LLM_PROVIDER_ORDER": "anthropic,openai"})
    assert gw.complete(req(data_class="non_personal")).provider == "anthropic"


def test_cost_from_price_table():
    gw, _ = make({"AJ_LLM_PRICES": '{"gpt-test": [2.0, 8.0]}'})
    assert gw.complete(req(data_class="non_personal")).cost_usd == pytest.approx(0.006)
    gw2, _ = make({"AJ_LLM_PRICES": "not json"})
    assert gw2.complete(req(data_class="non_personal")).cost_usd is None


def test_logs_are_content_free(caplog):
    gw, _ = make(openai=[ErrorKind.QUOTA])
    with caplog.at_level(logging.INFO, logger="aj.llm_layer.gateway"):
        gw.complete(req(messages=(Message(role="user", content="TOP-SECRET-PROMPT"),),
                        system="SECRET-SYSTEM", data_class="non_personal"), app="calibre")
    text = caplog.text
    assert "app=calibre" in text and "provider=anthropic" in text and "openai/gpt-test:quota" in text
    assert "TOP-SECRET" not in text and "SECRET-SYSTEM" not in text and "sk-" not in text


def test_status_hides_keys():
    gw, _ = make()
    status = gw.status()
    assert status["openai"]["key_configured"] is True and status["openai"]["available"] is True
    assert "sk-o" not in repr(status) and "sk-o" not in repr(gw.config)


def test_request_defaults_to_personal_and_validates():
    assert req().data_class == "personal"
    with pytest.raises(ValueError):
        CompletionRequest(messages=(Message(role="assistant", content="x"),))
    with pytest.raises(ValueError):
        req(purpose="Has Spaces")
    with pytest.raises(ValueError):
        CompletionRequest(messages=(Message(role="user", content="x"),), unknown_field=1)
