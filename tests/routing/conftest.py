from decimal import Decimal
import pytest
from aj_llm_layer.routing.contracts import Model, Price, Evidence, Snapshot, Authorization
from aj_llm_layer.routing.tasks import Task, FAMILIES
from aj_llm_layer.routing.policies import Policy

@pytest.fixture
def bundle():
    now = 1_800_000_000
    price = Price(version='synthetic.prices.v1', expires_at=now+1000, currency='USD', input_per_million=Decimal('1'),
                  output_per_million=Decimal('2'), cached_per_million=Decimal('.1'))
    evidence = Evidence(version='synthetic.evaluation.v1', model_version='v1', families=frozenset(FAMILIES), source='synthetic', samples=1000,
                        successes=990, observed_at=now-100, latency_p95_ms=1000,
                        failure_rate=.005, timeout_rate=.005, measured_cost=Decimal('10'), currency='USD')
    model = Model(id='synthetic-a', provider='synthetic', version='v1', approved=True,
                  provider_approved=True, enabled=True, model_class='restricted',
                  capabilities=frozenset().union(*FAMILIES.values(), {'audio'}),
                  modalities=frozenset({'text','image','audio','document'}),
                  context_limit=200000, output_limit=10000, regions=frozenset({'UK'}),
                  residencies=frozenset({'UK'}), price=price, evidence=evidence)
    registry = Snapshot(version='synthetic.registry.v1', expires_at=now+1000, models=(model,))
    auth = Authorization(tenant_ref='a'*64, version='synthetic.auth.v1', expires_at=now+1000,
        region='UK', residency='UK', privacy_class='personal', allowed_models=frozenset({model.id}),
        allowed_strategies=frozenset({'balanced','quality_first','cost_efficient','latency_sensitive','restricted'}),
        budget_remaining=Decimal('10'), currency='USD', privacy_available=True,
        budget_available=True, feature_enabled=True, restricted_approved=True)
    task = Task(families=('writing',), criticality='normal', input_tokens=1000, output_tokens=100)
    policy = Policy(version='synthetic.policy.v1', approved=True, enabled=True)
    return task, registry, auth, policy, now
