from aj_llm_layer.routing.tasks import Task, classify
from aj_llm_layer.routing.complexity import assess

def band(**kwargs):
    t = Task(**kwargs)
    return assess(t, classify(t)).band

def test_short_difficult_long_simple():
    assert band(families=('reasoning',), criticality='normal', steps=20) == 'complex'
    assert band(families=('writing',), criticality='normal', input_tokens=100000) == 'simple'

def test_stakes_and_unknown():
    assert band(families=('financial_analysis',)) == 'high_stakes'
    assert band() == 'unknown'
    assert band(families=('writing',), criticality='high_stakes') == 'high_stakes'
