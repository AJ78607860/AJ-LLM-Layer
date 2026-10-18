"""CLI: python -m aj_llm_layer.routing.demo > report.json (fully synthetic)."""
from decimal import Decimal
import json
from .contracts import Price, Evidence, Model, Snapshot, Authorization
from .tasks import FAMILIES
from .simulation import SimulationRequest, simulate

NOW = 1_800_000_000

def fixture():
    models = []
    for name, price, success, latency in [('quality', '4', 995, 2000),
                                           ('economy', '.5', 930, 600),
                                           ('fast', '2', 970, 150)]:
        p = Price(version='SYNTHETIC-price-v1', expires_at=NOW+1000, currency='USD',
                  input_per_million=Decimal(price), output_per_million=Decimal(price)*2,
                  cached_per_million=Decimal(price)/10)
        e = Evidence(version='SYNTHETIC-evidence-v1', model_version='fixture-v1',
            families=frozenset(FAMILIES), source='synthetic', samples=1000, successes=success,
            observed_at=NOW-1, latency_p95_ms=latency, failure_rate=.01, timeout_rate=.005,
            measured_cost=Decimal(price)*10, currency='USD')
        models.append(Model(id=name,provider='synthetic',version='fixture-v1', approved=True,
            provider_approved=True, enabled=True, model_class='restricted',
            capabilities=frozenset().union(*FAMILIES.values()),modalities=frozenset({'text','image','document'}),
            context_limit=100000, output_limit=10000, regions=frozenset({'UK'}),
            residencies=frozenset({'UK'}), price=p,evidence=e))
    registry = Snapshot(version='SYNTHETIC-registry-v1',expires_at=NOW+1000,models=tuple(models))
    auth = Authorization(tenant_ref='a'*64,version='SYNTHETIC-auth-v1',expires_at=NOW+1000,
        region='UK',residency='UK',privacy_class='personal',allowed_models=frozenset(m.id for m in models),
        allowed_strategies=frozenset({'quality_first','balanced','cost_efficient','latency_sensitive','restricted'}),
        budget_remaining=Decimal('1'),currency='USD',privacy_available=True,budget_available=True,
        feature_enabled=True,restricted_approved=True)
    return registry,auth

def main():
    registry,auth=fixture()
    result = {'assumptions': ['All prices and quality values are invented synthetic fixtures.',
        'Costs charge each simulated attempt, including failures; transport failures retry once.',
        'Latency samples are synthetic uniform [0.5,1.5) times supplied p95; not calibrated.',
        'Terminal quality is sampled conditionally on transport success; failures scale independently.',
        'No real-world quality or savings claims; no provider calls.'],
        'base':simulate(SimulationRequest(),registry,auth,now=NOW),
        'failure_sensitivity':simulate(SimulationRequest(failure_multiplier=5.0),registry,auth,now=NOW)}
    print(json.dumps(result,sort_keys=True,indent=2))

if __name__ == '__main__':
    main()
