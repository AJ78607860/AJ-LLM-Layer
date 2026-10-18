from decimal import Decimal
from aj_llm_layer.routing.ranking import Ranked, rank

def test_stable_pareto_and_ties():
    rows = tuple(Ranked(model_id=m, quality_lower=.9, latency_p95_ms=l,
                       expected_cost=Decimal(c), cost_per_success=Decimal(c))
                 for m,l,c in [('b',100,'1'), ('a',100,'1'), ('c',200,'2')])
    for strategy in ['balanced', 'quality_first', 'cost_efficient', 'latency_sensitive', 'restricted']:
        assert rank(rows, strategy) == rank(tuple(reversed(rows)), strategy)
        assert rank(rows, strategy)[0].model_id == 'a'
        assert not rank(rows, strategy)[2].pareto
