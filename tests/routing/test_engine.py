from decimal import Decimal
import json
import pytest
from pydantic import ValidationError
from aj_llm_layer.routing.engine import route, route_from_m1
from aj_llm_layer.routing.economics import estimate
from aj_llm_layer.routing.simulation import SimulationRequest, simulate
from aj_llm_layer.routing.contracts import Snapshot


def run(b, **kwargs):
    task, registry, auth, policy, now = b
    return route(task.model_dump(), registry, auth, policy, now=now, correlation_id='test-1', simulation=True, **kwargs)

def test_roundtrip_replay(bundle):
    d = run(bundle)
    assert d.selected == 'synthetic-a'
    assert not d.execution_authorized
    assert d == run(bundle)
    assert d == type(d).model_validate_json(d.model_dump_json())
    assert 'prompt' not in d.model_dump_json()

@pytest.mark.parametrize('changes,reason', [
    ({'approved':False}, 'MODEL_NOT_APPROVED'),
    ({'provider_approved':False}, 'PROVIDER_NOT_APPROVED'),
    ({'retired':True}, 'MODEL_DISABLED'),
    ({'enabled':False}, 'MODEL_DISABLED'),
    ({'capabilities':frozenset()}, 'CAPABILITY_MISMATCH'),
    ({'context_limit':1}, 'CONTEXT_LIMIT_EXCEEDED'),
    ({'output_limit':1}, 'OUTPUT_LIMIT_EXCEEDED'),
    ({'regions':frozenset()}, 'REGION_RESTRICTED'),
    ({'residencies':frozenset()}, 'REGION_RESTRICTED'),
    ({'evidence':None}, 'INSUFFICIENT_EVIDENCE'),
    ({'price':None}, 'PRICING_UNAVAILABLE'),
])
def test_model_gates(bundle, changes, reason):
    task,r,a,p,n=bundle
    r=r.model_copy(update={'models':(r.models[0].model_copy(update=changes),)})
    d=run((task,r,a,p,n))
    assert d.selected is None
    assert reason in d.excluded['synthetic-a']
    assert not d.eligible

@pytest.mark.parametrize('changes,reason', [
    ({'privacy_available':False}, 'PRIVACY_UNAVAILABLE'),
    ({'budget_available':False}, 'BUDGET_UNAVAILABLE'),
    ({'feature_enabled':False}, 'FEATURE_DISABLED'),
    ({'expires_at':1}, 'AUTHORIZATION_EXPIRED'),
    ({'allowed_strategies':frozenset()}, 'STRATEGY_NOT_AUTHORIZED'),
])
def test_auth_gates(bundle, changes, reason):
    t,r,a,p,n=bundle
    d=run((t,r,a.model_copy(update=changes),p,n))
    assert d.selected is None and reason in d.reasons

@pytest.mark.parametrize('changes,reason', [
    ({'budget_remaining':Decimal('0')},'BUDGET_EXCEEDED'),
    ({'currency':'GBP'},'ECONOMIC_ESTIMATE_UNAVAILABLE'),
    ({'allowed_models':frozenset()},'PRIVACY_RESTRICTED'),
    ({'region':'unknown'},'REGION_RESTRICTED'),
])
def test_financial_policy(bundle, changes, reason):
    t,r,a,p,n=bundle
    d=run((t,r,a.model_copy(update=changes),p,n))
    assert d.selected is None and reason in d.excluded['synthetic-a']

@pytest.mark.parametrize('changes', [{'samples':1,'successes':1}, {'successes':1}, {'observed_at':1}, {'observed_at':2_000_000_000}, {'failure_rate':.5}])
def test_evidence(bundle, changes):
    t,r,a,p,n=bundle
    m=r.models[0]
    m=m.model_copy(update={'evidence':m.evidence.model_copy(update=changes)})
    assert run((t,r.model_copy(update={'models':(m,)}),a,p,n)).selected is None

def test_missing_invalid_unknown_and_expiry(bundle):
    t,r,a,p,n=bundle
    for data in [{}, {'prompt':'secret'}, {'families':('injected',)}, {'input_tokens':float('nan')}]:
        d=route(data,r,a,p,now=n,correlation_id='test',simulation=True)
        assert d.selected is None and 'secret' not in d.model_dump_json()
    assert run((t,None,a,p,n)).selected is None
    assert run((t,r,None,p,n)).selected is None
    assert run((t,r.model_copy(update={'expires_at':n}),a,p,n)).selected is None
    assert run((t,r.model_copy(update={'models':()}),a,p,n)).selected is None

def test_high_stakes_stricter(bundle):
    t,r,a,p,n=bundle
    t=t.model_copy(update={'criticality':'high_stakes'})
    assert run((t,r,a,p,n)).selected
    assert run((t,r,a.model_copy(update={'restricted_approved':False}),p,n)).selected is None
    m=r.models[0].model_copy(update={'model_class':'standard'})
    assert run((t,r.model_copy(update={'models':(m,)}),a,p,n)).selected is None

def test_synthetic_evidence_never_real(bundle):
    t,r,a,p,n=bundle
    assert route(t.model_dump(),r,a,p,now=n,correlation_id='test').selected is None

def test_retry_cached_and_measured_cost(bundle):
    t,r,a,p,n=bundle
    cost=estimate(t,r.models[0],1,'USD')
    assert cost.expected_request_cost == Decimal('.00121200')
    assert cost.worst_case_cost == Decimal('.00240000')
    assert cost.measured_cost_per_success == Decimal('.01010102')
    cached=estimate(t.model_copy(update={'cached_tokens':1000}),r.models[0],0,'USD')
    assert cached.expected_request_cost == Decimal('.00030000')
    with pytest.raises(ValueError):
        estimate(t.model_copy(update={'cached_tokens':1001}),r.models[0],1,'USD')

def test_simulation_replay_budget_and_failure(bundle):
    _,r,a,_,n=bundle
    req=SimulationRequest(tasks=20)
    x=simulate(req,r,a,now=n)
    assert x==simulate(req,r,a,now=n)
    assert x!=simulate(SimulationRequest(tasks=20,seed=1),r,a,now=n)
    empty=simulate(req,r,a.model_copy(update={'budget_remaining':Decimal('0')}),now=n)
    assert all(v['rejections']==20 for v in empty['strategies'].values())
    assert len(x['strategies'])==5
    with pytest.raises(ValidationError):
        SimulationRequest(tasks=1001)

def test_adapter_outage_and_tenant_binding(bundle):
    t,r,a,p,n=bundle
    class Adapter:
        def registry(self): return r
        def authorize(self,*args): return a
    d=route_from_m1(t.model_dump(),Adapter(),tenant_ref='b'*64,feature='test',platform=p,now=n,correlation_id='test')
    assert d.selected is None
    class Outage:
        def registry(self): raise TimeoutError('secret')
    d=route_from_m1(t.model_dump(),Outage(),tenant_ref='a'*64,feature='test',platform=p,now=n,correlation_id='test')
    assert d.selected is None and 'secret' not in d.model_dump_json()

def test_registry_poisoning(bundle):
    _,r,_,_,_=bundle
    with pytest.raises(ValidationError):
        Snapshot(version='x',expires_at=1,models=(r.models[0],r.models[0]))

def test_corrupt_snapshot_and_policy_fail_closed(bundle):
    t,r,a,p,n=bundle
    corrupt=r.model_copy(update={'models':('poison',)})
    assert run((t,corrupt,a,p,n)).selected is None
    tenant=p.model_copy(update={'min_quality':float('nan')})
    assert run(bundle,tenant_policy=tenant).selected is None
    assert run((t,r,a,p.model_copy(update={'min_quality':float('nan')}),n)).selected is None

def test_no_sensitive_unknown_labels(bundle):
    t,r,a,p,n=bundle
    t=t.model_copy(update={'families':('private email alice@example.test',)})
    d=run((t,r,a,p,n))
    assert d.selected is None
    assert 'alice' not in d.model_dump_json()

def test_evidence_bound_to_model_and_family_and_price_expiry(bundle):
    t,r,a,p,n=bundle
    m=r.models[0]
    for evidence in [m.evidence.model_copy(update={'families':frozenset({'coding'})}),
                     m.evidence.model_copy(update={'model_version':'wrong'})]:
        bad=m.model_copy(update={'evidence':evidence})
        assert run((t,r.model_copy(update={'models':(bad,)}),a,p,n)).selected is None
    bad=m.model_copy(update={'price':m.price.model_copy(update={'expires_at':n})})
    assert run((t,r.model_copy(update={'models':(bad,)}),a,p,n)).selected is None

def test_permutations_and_candidate_scale(bundle):
    from itertools import permutations
    t,r,a,p,n=bundle
    models=tuple(r.models[0].model_copy(update={'id':f'model-{i}'}) for i in range(3))
    a=a.model_copy(update={'allowed_models':frozenset(m.id for m in models)})
    ids=set()
    for ordering in permutations(models):
        d=run((t,r.model_copy(update={'models':ordering}),a,p,n))
        ids.add(d.decision_id)
        assert d.selected=='model-0'
    assert len(ids)==1
    models=tuple(r.models[0].model_copy(update={'id':f'model-{i:04}'}) for i in range(200))
    a=a.model_copy(update={'allowed_models':frozenset(m.id for m in models)})
    assert len(run((t,r.model_copy(update={'models':models}),a,p,n)).eligible)==200

def test_no_network_or_execution(bundle, monkeypatch):
    import socket
    def forbidden(*args,**kwargs): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket,'socket',forbidden)
    assert run(bundle).selected
    _,r,a,_,n=bundle
    simulate(SimulationRequest(tasks=3),r,a,now=n)

def test_price_zero_rounding_version_change(bundle):
    t,r,a,p,n=bundle
    m=r.models[0]
    zero=m.price.model_copy(update={'input_per_million':Decimal(0),'output_per_million':Decimal(0)})
    assert estimate(t,m.model_copy(update={'price':zero}),1,'USD').worst_case_cost==0
    tiny=m.price.model_copy(update={'input_per_million':Decimal('.0000000001'),'output_per_million':Decimal(0)})
    assert estimate(t,m.model_copy(update={'price':tiny}),0,'USD').expected_request_cost==Decimal('.00000001')
    changed=m.model_copy(update={'price':m.price.model_copy(update={'version':'new-price'})})
    assert run(bundle).decision_id!=run((t,r.model_copy(update={'models':(changed,)}),a,p,n)).decision_id

def test_profiles_and_tenant_restrictions(bundle):
    t,r,a,p,n=bundle
    for strategy in ['balanced','quality_first','cost_efficient','latency_sensitive','restricted']:
        assert run((t,r,a,p.model_copy(update={'strategy':strategy}),n)).selected
    assert run(bundle,tenant_policy=p.model_copy(update={'min_quality':.1})).selected is None
    assert run(bundle,tenant_policy=p.model_copy(update={'min_quality':.99})).selected is None

def test_cross_process_determinism():
    import os, subprocess, sys
    program='''from aj_llm_layer.routing.demo import fixture,NOW
from aj_llm_layer.routing.tasks import Task
from aj_llm_layer.routing.policies import Policy
from aj_llm_layer.routing.engine import route
r,a=fixture()
d=route(Task(families=('coding','reasoning'),criticality='normal').model_dump(),r,a,Policy(version='v1',approved=True,enabled=True),now=NOW,correlation_id='replay',simulation=True)
print(d.model_dump_json())'''
    outputs=[subprocess.check_output([sys.executable,'-c',program],env={**os.environ,'PYTHONHASHSEED':str(seed)}) for seed in (1,2,123)]
    assert len(set(outputs))==1

def test_simulation_constructed_limit_and_retry_math(bundle):
    _,r,a,_,n=bundle
    with pytest.raises(ValidationError):
        simulate(SimulationRequest.model_construct(tasks=1000000000),r,a,now=n)
    t= bundle[0]
    m=r.models[0]
    c0=estimate(t,m,0,'USD')
    c1=estimate(t,m,1,'USD')
    assert c1.validated_completion_probability_lower > c0.validated_completion_probability_lower
    assert c0.expected_cost_per_success == c1.expected_cost_per_success
