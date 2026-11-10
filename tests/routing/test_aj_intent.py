"""aj.intent.v1 synthetic shadow comparison: privacy, continuity, scope and content-free contract.

Synthetic only. The registry below is fictional test data: prices and evidence are invented, and
``fixture_open`` is a fictional provider used to prove that a cheaper non-approved route can never be
selected for personal or sensitive content. No provider is called.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from aj_llm_layer.routing.aj_intent import (PROVIDERS_FOR_CLASS, compare, effective_class, intent_digest,
                                            invariant_violations, parse_intent)
from aj_llm_layer.routing.contracts import Authorization, Evidence, Model, Price, Snapshot
from aj_llm_layer.routing.policies import Policy
from aj_llm_layer.routing.tasks import FAMILIES

NOW = 1_800_000_000
FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'aj_intents.v1.json'
# Pinned with the AJ-side copy (AJ tests/fixtures/llm_layer/aj_intents.v1.json). Change both together.
FIXTURE_SHA256 = '2edff771c91e964211f7dadb514004105c0b4166a3b61a480a93651ff6c53341'


def _model(model_id: str, provider: str, inp: str, out: str, successes: int = 980) -> Model:
    price = Price(version='synthetic.prices.v1', expires_at=NOW + 1000, currency='USD',
                  input_per_million=Decimal(inp), output_per_million=Decimal(out),
                  cached_per_million=Decimal(inp) / 10)
    evidence = Evidence(version='synthetic.evaluation.v1', model_version='v1', families=frozenset(FAMILIES),
                        source='synthetic', samples=1000, successes=successes, observed_at=NOW - 100,
                        latency_p95_ms=1500, failure_rate=.005, timeout_rate=.005,
                        measured_cost=Decimal('5'), currency='USD')
    return Model(id=model_id, provider=provider, version='v1', approved=True, provider_approved=True,
                 enabled=True, capabilities=frozenset().union(*FAMILIES.values()),
                 modalities=frozenset({'text', 'image', 'audio', 'document'}), context_limit=400_000,
                 output_limit=64_000, regions=frozenset({'UK'}), residencies=frozenset({'UK'}),
                 price=price, evidence=evidence)


def synthetic_world():
    """Synthetic registry, authorisation and policy (also used by AJ's offline harness)."""
    registry = Snapshot(version='synthetic.registry.aj-shadow.v1', expires_at=NOW + 1000, models=(
        _model('gpt-5.6', 'openai', '4', '16'),
        _model('gpt-5.6-luna', 'openai', '1', '4'),
        _model('fixture-open-small', 'fixture_open', '0.1', '0.4'),
    ))
    auth = Authorization(tenant_ref='b' * 64, version='synthetic.auth.v1', expires_at=NOW + 1000, region='UK',
                         residency='UK', privacy_class='personal',
                         allowed_models=frozenset(m.id for m in registry.models),
                         allowed_strategies=frozenset({'balanced', 'cost_efficient', 'quality_first'}),
                         budget_remaining=Decimal('5'), currency='USD', privacy_available=True,
                         budget_available=True, feature_enabled=True, restricted_approved=False)
    policy = Policy(version='synthetic.policy.cost_efficient.v1', strategy='cost_efficient', approved=True,
                    enabled=True)
    return registry, auth, policy


@pytest.fixture
def world():
    return synthetic_world()


def intent(**over):
    base = dict(synthetic=True, purpose='core_conversation', data_class='sensitive',
                cortex_task_class='general_reasoning', tier='standard', api_family='responses',
                modalities=('text',), tools=True, structured=False, continuity='none',
                input_tokens_bucket=4096, output_tokens_bucket=1024,
                incumbent={'provider': 'openai', 'model': 'gpt-5.6'})
    base.update(over)
    return base


def run(world, data):
    registry, auth, policy = world
    return compare(data, registry, auth, policy, now=NOW, correlation_id='synthetic-aj-intent')


def test_sensitive_conversation_never_reaches_cheaper_unapproved_provider(world):
    result = run(world, intent())
    assert result.layer_selected in {'gpt-5.6', 'gpt-5.6-luna'}
    assert result.layer_selected != 'fixture-open-small'
    assert result.invariant_violations == ()
    assert result.execution_authorized is False and result.budget_reserved is False


def test_caller_cannot_downgrade_a_sensitive_purpose(world):
    result = run(world, intent(data_class='public'))
    assert result.effective_data_class == 'sensitive'
    assert result.layer_selected != 'fixture-open-small'


def test_public_purpose_may_use_any_approved_provider(world):
    result = run(world, intent(purpose='public_web_synthesis', data_class='public', cortex_task_class='none',
                               tools=False))
    assert result.effective_data_class == 'public'
    assert result.verdict == 'ALTERNATIVE' and result.layer_selected == 'fixture-open-small'
    assert result.expected_cost_delta is not None and result.expected_cost_delta < 0


def test_tool_loop_is_pinned_to_the_incumbent_model(world):
    result = run(world, intent(continuity='tool_loop'))
    assert result.verdict == 'AGREE' and result.layer_selected == 'gpt-5.6'


def test_response_chain_stays_with_the_incumbent_provider(world):
    result = run(world, intent(continuity='response_chain', data_class='sensitive'))
    assert result.layer_selected in {'gpt-5.6', 'gpt-5.6-luna'}


@pytest.mark.parametrize('family', ['transcription', 'embedding', 'realtime', 'computer_use'])
def test_non_text_generation_families_are_out_of_scope(world, family):
    result = run(world, intent(purpose='transcription', api_family=family, cortex_task_class='none'))
    assert result.verdict == 'OUT_OF_SCOPE' and result.layer_selected is None


@pytest.mark.parametrize('task_class', ['financial_interpretation', 'safety_critical'])
def test_high_stakes_needs_restricted_approval(world, task_class):
    result = run(world, intent(cortex_task_class=task_class))
    assert result.verdict == 'NO_ELIGIBLE_MODEL'
    assert 'HIGH_STAKES_RESTRICTED' in result.reasons or 'NO_ELIGIBLE_MODEL' in result.reasons


@pytest.mark.parametrize('bad', [
    {'prompt': 'SYNTHETIC hello'},                 # content fields are not part of the contract
    {'user_id': 'usr_1'},
    {'channel': 'whatsapp'},
    {'synthetic': False},                           # live-derived intents are refused in v1
    {'input_tokens_bucket': 1000},                  # exact sizes refused: buckets only
    {'purpose': 'unregistered_task'},
    {'modalities': ('text', 'image', 'text')},
    {'incumbent': {'provider': 'openai', 'model': 'gpt 5.6; drop'}},
])
def test_invalid_or_content_bearing_intents_are_refused(world, bad):
    result = run(world, intent(**bad))
    assert result.verdict == 'INVALID_INTENT' and result.layer_selected is None


def test_post_route_invariants_catch_a_forged_selection(world):
    registry, _, _ = world
    sensitive = parse_intent(intent(continuity='tool_loop'))
    assert 'PRIVACY_DOWNGRADE' in invariant_violations(sensitive, registry, 'fixture-open-small')
    assert 'TOOL_LOOP_MODEL_SWITCH' in invariant_violations(sensitive, registry, 'gpt-5.6-luna')
    assert 'PRIVACY_DOWNGRADE' in invariant_violations(sensitive, registry, 'not-in-registry')
    chain = parse_intent(intent(continuity='response_chain', purpose='public_web_synthesis',
                                                  data_class='public'))
    assert 'CHAIN_PROVIDER_SWITCH' in invariant_violations(chain, registry, 'fixture-open-small')


def test_comparison_is_deterministic_and_content_free(world):
    a, b = run(world, intent()), run(world, intent())
    assert a == b and a.decision_id
    dumped = json.dumps(a.model_dump(mode='json'))
    for forbidden in ('prompt', 'user', 'tenant', 'channel', 'text'):
        assert forbidden not in dumped


def test_adr025_mirror_is_openai_only_for_personal_data():
    assert PROVIDERS_FOR_CLASS['sensitive'] == frozenset({'openai'})
    assert PROVIDERS_FOR_CLASS['personal'] == frozenset({'openai'})


def test_aj_golden_intents_validate_and_never_violate_invariants(world):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256, 'fixture drifted from the AJ-side copy'
    fixture = json.loads(raw)
    assert fixture['schema_version'] == 'aj.intent.v1' and fixture['synthetic'] is True
    verdicts = {}
    for case in fixture['cases']:
        parsed = parse_intent(case['intent'])
        assert intent_digest(parsed) == case['digest']
        result = run(world, case['intent'])
        assert result.verdict != 'INVALID_INTENT', case['id']
        assert result.invariant_violations == (), case['id']
        if effective_class(parsed) != 'public':
            assert result.layer_selected in (None, 'gpt-5.6', 'gpt-5.6-luna'), case['id']
        verdicts[case['id']] = result.verdict
    assert verdicts, 'fixture is empty'
