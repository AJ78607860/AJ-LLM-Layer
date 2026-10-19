import pytest

from aj_llm_layer.evaluation import TaskCase, validate
from aj_llm_layer.execution import DisabledAdapter, Request
from aj_llm_layer.learning import Bandit, Outcome
from aj_llm_layer.ranking import Candidate, rank


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), True, 'bad', None, 10 ** 1000])
def test_invalid_routing_requirements_fail_closed(value):
    with pytest.raises(ValueError):
        rank((), min_quality=.9, max_latency_p95_ms=value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, 'bad', None, 10 ** 1000])
def test_invalid_candidate_metrics_are_excluded(value):
    assert rank((Candidate('p', 'm', value, .99, 1),), min_quality=.9, max_latency_p95_ms=10) == ()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, 'bad', None, 10 ** 1000])
def test_bad_outcomes_do_not_mutate_statistics(value):
    stats = Bandit()
    with pytest.raises(ValueError):
        stats.observe(Outcome('math', 'model', True, True, value, 1))
    assert stats.counts == stats.successes == stats.costs == {}


def test_aggregate_overflow_is_atomic():
    stats = Bandit()
    item = Outcome('math', 'model', True, True, 1e308, 1)
    stats.observe(item)
    with pytest.raises(ValueError):
        stats.observe(item)
    assert stats.counts[('math', 'model')] == 1
    assert stats.costs[('math', 'model')] == 1e308


@pytest.mark.parametrize('response,expected', [('NaN', 'NaN'), ('Infinity', 'Infinity'), ('1e999', '1e999'), ('true', '1'), ('{"a":1,"a":2}', '{"a":2}'), ('1' * 65537, '1'), ('[' * 2000, '[]')])
def test_invalid_or_ambiguous_json_cannot_score_success(response, expected):
    assert not validate(TaskCase('id', 'test', 'synthetic', expected, 'json'), response)


def test_json_object_order_is_irrelevant():
    assert validate(TaskCase('id', 'test', 'synthetic', '{"a":1,"b":2}', 'json'), '{"b":2,"a":1}')


def test_execution_remains_disabled():
    with pytest.raises(PermissionError):
        DisabledAdapter().complete(Request('model', 'test', 'synthetic'))
