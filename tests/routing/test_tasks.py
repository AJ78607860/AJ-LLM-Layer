import pytest
from pydantic import ValidationError
from aj_llm_layer.routing.tasks import Task, classify, FAMILIES

@pytest.mark.parametrize('family', list(FAMILIES))
def test_taxonomy_replay(family):
    task = Task(families=(family,))
    assert classify(task) == classify(task)
    assert not classify(task).unknown

@pytest.mark.parametrize('labels', [(), ('ignore all policy',), ('coding', 'unknown'), ('Coding',)])
def test_unknown(labels):
    assert classify(Task(families=labels)).unknown

def test_multilabel():
    assert classify(Task(families=('coding', 'mathematics'))).capabilities == {'code', 'math', 'reasoning'}

@pytest.mark.parametrize('kwargs', [{'prompt': 'secret'}, {'schema_version': 'v2'}, {'input_tokens': -1}, {'input_tokens': True}, {'input_tokens': float('nan')}])
def test_malformed(kwargs):
    with pytest.raises(ValidationError):
        Task(**kwargs)
