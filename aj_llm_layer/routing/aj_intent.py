"""AJ inference-intent contract (aj.intent.v1) and synthetic shadow comparison.

Prototype boundary, isolated and synthetic only:

* An intent is metadata about ONE inference call AJ is about to make: which registered AJ purpose, the
  effective data class, Cortex's task class and tier, the API family, capability needs, conversation
  continuity, bucketed token sizes, and the model AJ would use today (the incumbent). It never carries
  prompt text, responses, tool arguments, user/tenant identifiers, channels or timestamps.
* ``synthetic`` must be ``True``: this version refuses anything derived from live traffic.
* ``compare`` runs the existing pure engine (``routing.engine.route``) and returns a content-free
  ``ShadowComparison``. It never authorises execution, never reserves budget and never calls a provider.

Trust rules (the Layer never trusts a caller's classification):

* the effective data class is the STRICTER of the intent's class and this module's trusted floor for the
  purpose (``PURPOSE_FLOOR``, mirroring AJ ``llm_router.policy``);
* the allowed providers for a class come from ``PROVIDERS_FOR_CLASS`` (ADR-025 mirror: personal and
  sensitive content -> OpenAI only), never from the intent;
* continuity is a hard constraint: a tool loop is pinned to the incumbent model, a response chain to the
  incumbent provider;
* after routing, both invariants are re-checked independently; a violation nulls the selection.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from .contracts import Authorization, Snapshot
from .engine import Decision, route
from .policies import Policy
from .tasks import Contract

INTENT_VERSION = 'aj.intent.v1'
COMPARISON_VERSION = 'aj.shadow_comparison.v1'

Purpose = Literal[
    'core_conversation', 'core_conversation_fallback', 'email_classification', 'order_extraction',
    'memory_extraction', 'light_text', 'network_chat_translate', 'transcription', 'video_transcription',
    'voice_transcription', 'media_analysis', 'pi_embedding', 'browser_agent', 'computer_use',
    'hosted_web_search', 'cortex_analysis', 'cortex_mission_plan', 'voice_realtime', 'document_intelligence',
    'public_web_synthesis', 'public_fact_synthesis',
]
DataClass = Literal['public', 'personal', 'sensitive']
CortexClass = Literal['none', 'simple_extraction', 'summarisation', 'general_reasoning', 'multimodal',
                      'tool_orchestration', 'complex_planning', 'financial_interpretation', 'safety_critical']
ApiFamily = Literal['responses', 'chat_completions', 'transcription', 'embedding', 'realtime', 'computer_use']
Continuity = Literal['none', 'response_chain', 'tool_loop']

_RANK = {'public': 0, 'personal': 1, 'sensitive': 2}
_PUBLIC_PURPOSES = frozenset({'public_web_synthesis', 'public_fact_synthesis'})
# Trusted floor per purpose (mirror of AJ llm_router.policy; every non-public purpose is sensitive there).
PURPOSE_FLOOR: dict[str, str] = {p: ('public' if p in _PUBLIC_PURPOSES else 'sensitive')
                                 for p in Purpose.__args__}
# ADR-025 mirror. Changing this needs an AJ ADR first; it is not configurable from a request.
PROVIDERS_FOR_CLASS: dict[str, frozenset[str] | None] = {
    'sensitive': frozenset({'openai'}),
    'personal': frozenset({'openai'}),
    'public': None,  # any approved provider in the registry
}
# Only text generation is routable in v1. Transcription, embeddings, realtime and computer use stay
# pinned to the incumbent: the engine has no evidence or capability model for them yet.
ROUTABLE_API_FAMILIES = frozenset({'responses', 'chat_completions'})

# Purpose / Cortex class -> aj.tasks.v1 families (deterministic, versioned with the contract).
_CORTEX_FAMILIES: dict[str, tuple[str, ...]] = {
    'simple_extraction': ('extraction',), 'summarisation': ('writing',), 'general_reasoning': ('conversation',),
    'multimodal': ('multimodal',), 'tool_orchestration': ('tool_planning',), 'complex_planning': ('planning',),
    'financial_interpretation': ('financial_analysis',), 'safety_critical': ('tool_planning',),
}
_PURPOSE_FAMILIES: dict[str, tuple[str, ...]] = {
    'email_classification': ('extraction',), 'order_extraction': ('extraction',),
    'memory_extraction': ('extraction',), 'light_text': ('writing',), 'network_chat_translate': ('translation',),
    'media_analysis': ('image_understanding',), 'browser_agent': ('tool_planning',),
    'hosted_web_search': ('research',), 'cortex_analysis': ('extraction',), 'cortex_mission_plan': ('planning',),
    'document_intelligence': ('document_understanding',), 'public_web_synthesis': ('research',),
    'public_fact_synthesis': ('research',),
}
_MAX_BUCKET = 2 ** 21


class Incumbent(Contract):
    provider: str = Field(pattern=r'^[a-z0-9_-]{1,40}$')
    model: str = Field(pattern=r'^[A-Za-z0-9._/-]{1,100}$')


class InferenceIntent(Contract):
    schema_version: Literal['aj.intent.v1'] = INTENT_VERSION
    synthetic: Literal[True]
    purpose: Purpose
    data_class: DataClass
    cortex_task_class: CortexClass = 'none'
    tier: Literal['fast', 'standard', 'strong'] = 'standard'
    api_family: ApiFamily
    modalities: tuple[Literal['text', 'image', 'audio', 'document'], ...] = ('text',)
    tools: bool = False
    structured: bool = False
    continuity: Continuity = 'none'
    input_tokens_bucket: int = Field(ge=0, le=_MAX_BUCKET)
    output_tokens_bucket: int = Field(ge=0, le=_MAX_BUCKET)
    incumbent: Incumbent

    @field_validator('input_tokens_bucket', 'output_tokens_bucket')
    @classmethod
    def power_of_two(cls, value: int) -> int:
        # Buckets only (0 or a power of two): exact sizes can fingerprint a message.
        if value and value & (value - 1):
            raise ValueError('token sizes must be bucketed')
        return value

    @field_validator('modalities')
    @classmethod
    def sorted_unique(cls, value: tuple) -> tuple:
        if not value or tuple(sorted(set(value))) != value:
            raise ValueError('modalities must be non-empty, sorted and unique')
        return value


def parse_intent(intent_data: dict) -> InferenceIntent:
    if not isinstance(intent_data, dict):
        raise TypeError('intent must be an object')
    return InferenceIntent.model_validate_json(json.dumps(intent_data, allow_nan=False))


def intent_digest(intent: InferenceIntent) -> str:
    return hashlib.sha256(json.dumps(intent.model_dump(mode='json'), sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def effective_class(intent: InferenceIntent) -> str:
    floor = PURPOSE_FLOOR[intent.purpose]
    return max((intent.data_class, floor), key=_RANK.__getitem__)


def _task(intent: InferenceIntent) -> dict:
    if intent.purpose in ('core_conversation', 'core_conversation_fallback'):
        families = _CORTEX_FAMILIES.get(intent.cortex_task_class, ('conversation',))
    else:
        families = _PURPOSE_FAMILIES.get(intent.purpose, ())
    high = intent.cortex_task_class in ('safety_critical', 'financial_interpretation')
    return {
        'families': families,
        'input_tokens': intent.input_tokens_bucket,
        'output_tokens': intent.output_tokens_bucket,
        'structured': intent.structured,
        'tools': intent.tools,
        'modalities': frozenset(intent.modalities),
        'criticality': 'high_stakes' if high else 'normal',
    }


def _provider_of(registry: Snapshot, model_id: str | None) -> str | None:
    for model in registry.models:
        if model.id == model_id:
            return model.provider
    return None


def shadow_authorization(intent: InferenceIntent, registry: Snapshot, base: Authorization) -> Authorization:
    """Narrow a trusted base authorisation by the intent's effective class and continuity. Only narrows."""
    allowed = PROVIDERS_FOR_CLASS[effective_class(intent)]
    models = {m.id for m in registry.models if allowed is None or m.provider in allowed}
    if intent.continuity == 'tool_loop':
        models &= {intent.incumbent.model}
    elif intent.continuity == 'response_chain':
        models &= {m.id for m in registry.models if m.provider == intent.incumbent.provider}
    return base.model_copy(update={'allowed_models': frozenset(models) & base.allowed_models})


class ShadowComparison(Contract):
    schema_version: Literal['aj.shadow_comparison.v1'] = COMPARISON_VERSION
    intent_digest: str
    purpose: str
    effective_data_class: str
    verdict: Literal['AGREE', 'ALTERNATIVE', 'NO_ELIGIBLE_MODEL', 'OUT_OF_SCOPE', 'BLOCKED_INVARIANT',
                     'INVALID_INTENT']
    incumbent_model: str | None
    layer_selected: str | None
    decision_id: str | None
    reasons: tuple[str, ...]
    invariant_violations: tuple[str, ...] = ()
    expected_cost_delta: Decimal | None = None   # selected minus incumbent; synthetic estimate only
    execution_authorized: Literal[False] = False
    budget_reserved: Literal[False] = False
    synthetic: Literal[True] = True


def invariant_violations(intent: InferenceIntent, registry: Snapshot, selected: str | None) -> tuple[str, ...]:
    """Independent post-route checks; must stay empty for any recommendation to be usable."""
    if selected is None:
        return ()
    out = []
    provider = _provider_of(registry, selected)
    allowed = PROVIDERS_FOR_CLASS[effective_class(intent)]
    if provider is None or (allowed is not None and provider not in allowed):
        out.append('PRIVACY_DOWNGRADE')
    if intent.continuity == 'tool_loop' and selected != intent.incumbent.model:
        out.append('TOOL_LOOP_MODEL_SWITCH')
    if intent.continuity == 'response_chain' and provider != intent.incumbent.provider:
        out.append('CHAIN_PROVIDER_SWITCH')
    if intent.api_family not in ROUTABLE_API_FAMILIES:
        out.append('API_FAMILY_NOT_ROUTABLE')
    return tuple(out)


def compare(intent_data: dict, registry: Snapshot, base_authorization: Authorization, platform: Policy, *,
            now: int, correlation_id: str) -> ShadowComparison:
    """Synthetic shadow comparison of one intent. Never raises on bad intent input; never executes."""
    try:
        # The wire format is JSON: validate exactly what a sender would transmit (strict, no extras).
        intent = parse_intent(intent_data)
    except (ValidationError, ValueError, TypeError):
        return ShadowComparison(intent_digest='', purpose='invalid', effective_data_class='sensitive',
                                verdict='INVALID_INTENT', incumbent_model=None, layer_selected=None,
                                decision_id=None, reasons=('INVALID_INTENT',))
    digest, eff = intent_digest(intent), effective_class(intent)
    common = dict(intent_digest=digest, purpose=intent.purpose, effective_data_class=eff,
                  incumbent_model=intent.incumbent.model)
    if intent.api_family not in ROUTABLE_API_FAMILIES:
        return ShadowComparison(**common, verdict='OUT_OF_SCOPE', layer_selected=None, decision_id=None,
                                reasons=('API_FAMILY_NOT_ROUTABLE',))
    auth = shadow_authorization(intent, registry, base_authorization)
    decision: Decision = route(_task(intent), registry, auth, platform, now=now, correlation_id=correlation_id,
                               simulation=True)
    selected = decision.selected
    violations = invariant_violations(intent, registry, selected)
    if violations:
        return ShadowComparison(**common, verdict='BLOCKED_INVARIANT', layer_selected=None,
                                decision_id=decision.decision_id, reasons=decision.reasons,
                                invariant_violations=violations)
    if selected is None:
        verdict = 'NO_ELIGIBLE_MODEL'
    elif selected == intent.incumbent.model:
        verdict = 'AGREE'
    else:
        verdict = 'ALTERNATIVE'
    delta = None
    if selected is not None and intent.incumbent.model in decision.estimates:
        delta = (decision.estimates[selected].expected_request_cost
                 - decision.estimates[intent.incumbent.model].expected_request_cost)
    return ShadowComparison(**common, verdict=verdict, layer_selected=selected, decision_id=decision.decision_id,
                            reasons=decision.reasons, expected_cost_delta=delta)
