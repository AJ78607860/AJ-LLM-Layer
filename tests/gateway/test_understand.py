"""The Layer reads the question itself: task family and tier, with no model call."""
import json
from pathlib import Path

import pytest

from aj_llm_layer.gateway import CompletionRequest, ErrorKind, Gateway, Message, SelectorSettings, load_config
from aj_llm_layer.gateway.understand import resolve, understand

from .test_selection import ENV, ModelFake, Rng
from .test_router import Clock

CASES = json.loads((Path(__file__).parent.parent / "fixtures" / "understand_cases.v1.json").read_text())


def u(text, **kw):
    return understand((Message(role="user", content=text),), **kw)


def test_labelled_questions_accuracy():
    fam = sum(u(c["question"]).task_family == c["task_family"] for c in CASES)
    tier = sum(u(c["question"]).tier == c["tier"] for c in CASES)
    # Measured on 35 hand-labelled questions: 34/35 family, 35/35 tier. Guard against regressions.
    assert fam / len(CASES) >= 0.9, fam
    assert tier / len(CASES) >= 0.95, tier


@pytest.mark.parametrize("question,family,tier", [
    ("hi", "conversation", "fast"),
    ("Translate good morning into Spanish", "translation", "fast"),
    ("Prove that there are infinitely many primes", "mathematics", "smart"),
    ("What dosage of paracetamol is safe?", "reasoning", "smart"),
    ("Analyse my pension and investment portfolio", "financial_analysis", "smart"),
])
def test_key_routing_decisions(question, family, tier):
    r = u(question)
    assert (r.task_family, r.tier) == (family, tier)


def test_signals_are_rule_names_not_text():
    r = u("Please translate my secret password hunter2 into French")
    assert all("hunter2" not in s and "password" not in s for s in r.signals)
    assert r.version == "aj.understand.v1"


def test_json_mode_and_long_input():
    assert u("Sam is 31", json_mode=True).task_family == "structured_data"
    assert u("x " * 70_000).tier == "smart"  # ~35k tokens


def test_app_labels_always_win():
    req = CompletionRequest(messages=(Message(role="user", content="hi"),),
                            task_family="coding", tier="smart")
    resolved, info = resolve(req)
    assert (resolved.task_family, resolved.tier) == ("coding", "smart")
    assert info == {"family_source": "app", "tier_source": "app"}
    partly, info = resolve(req.model_copy(update={"tier": "auto"}))
    assert partly.task_family == "coding" and partly.tier == "fast" and info["tier_source"] == "auto"


def build(anthropic=None, openai=None, text="ok"):
    clock = Clock()
    clock.now = 1_790_000_000
    fakes = {"openai": ModelFake("openai", openai, text), "anthropic": ModelFake("anthropic", anthropic, text)}
    return Gateway(load_config(ENV), providers=fakes, retry_delay=0, clock=clock, wall_clock=clock,
                   rng=Rng(1.0), settings=SelectorSettings(explore_rate=0)), fakes


def test_auto_request_routes_by_what_was_asked():
    gw, _ = build()
    easy = gw.complete(CompletionRequest(messages=(Message(role="user", content="thanks!"),),
                                         data_class="non_personal"))
    hard = gw.complete(CompletionRequest(messages=(Message(role="user", content=
                       "Prove that the square root of 2 is irrational, step by step"),), data_class="non_personal"))
    assert (easy.task_family, easy.tier, easy.model) == ("conversation", "fast", "claude-haiku-5-5")
    assert (hard.task_family, hard.tier, hard.model) == ("mathematics", "smart", "claude-opus-5-5")
    assert easy.understanding["family_source"] == "auto" and easy.understanding["signals"]
    # evidence is filed under the family the Layer read
    assert gw.ledger.summary("mathematics", "claude-opus-5-5")["calls"] == 1


class EmptyThenOk:
    """Cheap model returns an empty answer; stronger ones answer."""
    def __init__(self, name, empty_models):
        self.name, self.empty, self.calls = name, set(empty_models), []

    def complete(self, request, model):
        from aj_llm_layer.gateway.providers import RawCompletion
        from aj_llm_layer.gateway.types import Usage
        self.calls.append(model)
        return RawCompletion(text="" if model in self.empty else '{"ok": true}', finish_reason="stop",
                             usage=Usage(input_tokens=10, output_tokens=5))


def test_failed_check_escalates_to_stronger_tier():
    clock = Clock(); clock.now = 1_790_000_000
    fakes = {"anthropic": EmptyThenOk("anthropic", {"claude-haiku-5-5"}),
             "openai": EmptyThenOk("openai", {"gpt-6.1-terra"})}
    gw = Gateway(load_config(ENV), providers=fakes, retry_delay=0, clock=clock, wall_clock=clock,
                 rng=Rng(1.0), settings=SelectorSettings(explore_rate=0))
    out = gw.complete(CompletionRequest(messages=(Message(role="user", content="hi"),),
                                        data_class="non_personal", tier="fast"))
    assert out.model == "claude-sonnet-5-5" and out.selection == "escalated"
    assert fakes["openai"].calls == []  # same-tier terra skipped once escalating
    assert [a.outcome for a in out.attempts][:1] == ["failed_check"]
    assert gw.ledger.summary("conversation", "claude-haiku-5-5")["graded"] == 1


def test_if_nothing_passes_the_check_the_best_effort_answer_is_returned():
    clock = Clock(); clock.now = 1_790_000_000
    every = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
    fakes = {"anthropic": EmptyThenOk("anthropic", every)}
    gw = Gateway(load_config({"ANTHROPIC_API_KEY": "k"}), providers=fakes, retry_delay=0, clock=clock,
                 wall_clock=clock, rng=Rng(1.0), settings=SelectorSettings(explore_rate=0))
    out = gw.complete(CompletionRequest(messages=(Message(role="user", content="hi"),),
                                        data_class="non_personal", tier="fast"))
    assert out.model == "claude-haiku-5-5" and out.text == ""
    assert len(fakes["anthropic"].calls) == 3


# Review of PR #23 (Instinct, 25 Nov): unlabelled high-stakes questions reached the fast model.
HIGH_STAKES = [
    "I have chest pain and my left arm feels numb, what should I do?",
    "Should I take out a house loan or keep renting?",
    "My baby has a high fever and won't stop crying",
    "Can I break my lease early without being sued?",
    "How much can I borrow on a personal loan?",
    "I took too many sleeping pills",
    "My landlord is evicting me, what are my rights?",
    "Is it safe to mix alcohol with antidepressants?",
    "Should I pay off my credit card debt or invest?",
    "I think I'm having an allergic reaction, my throat is swelling",
    "Should I remortgage my house?",
    "Can my employer fire me for being pregnant?",
]


@pytest.mark.parametrize("question", HIGH_STAKES)
def test_high_stakes_questions_never_get_the_fast_model(question):
    r = u(question)
    assert r.risk == "high" and r.tier == "smart"


@pytest.mark.parametrize("question", ["What's the capital of Australia?", "Tell me a joke",
                                      "What should I do this weekend?"])
def test_unclassified_questions_default_to_standard_not_fast(question):
    assert u(question).tier == "standard"


def test_reported_cost_includes_every_billed_attempt():
    clock = Clock(); clock.now = 1_790_000_000
    fakes = {"anthropic": EmptyThenOk("anthropic", {"claude-haiku-5-5"}),
             "openai": EmptyThenOk("openai", {"gpt-6.1-terra"})}
    gw = Gateway(load_config(ENV), providers=fakes, retry_delay=0, clock=clock, wall_clock=clock,
                 rng=Rng(1.0), settings=SelectorSettings(explore_rate=0))
    out = gw.complete(CompletionRequest(messages=(Message(role="user", content="hi"),),
                                        data_class="non_personal", tier="fast"))
    cat = gw.catalog
    expected = cat.get("claude-haiku-5-5").cost(10, 5) + cat.get("claude-sonnet-5-5").cost(10, 5)
    assert out.cost_usd == pytest.approx(expected)
    assert gw.health.status("anthropic")["spend_this_month_usd"] == pytest.approx(expected, abs=1e-4)


def test_best_effort_answer_reports_total_cost():
    clock = Clock(); clock.now = 1_790_000_000
    every = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
    gw = Gateway(load_config({"ANTHROPIC_API_KEY": "k"}),
                 providers={"anthropic": EmptyThenOk("anthropic", every)}, retry_delay=0, clock=clock,
                 wall_clock=clock, rng=Rng(1.0), settings=SelectorSettings(explore_rate=0))
    out = gw.complete(CompletionRequest(messages=(Message(role="user", content="hi"),),
                                        data_class="non_personal", tier="fast"))
    assert out.cost_usd == pytest.approx(sum(gw.catalog.get(m).cost(10, 5) for m in every))
