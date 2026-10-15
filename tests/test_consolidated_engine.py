import pytest

from aj_llm_layer.learning import Bandit, Outcome
from aj_llm_layer.evaluation import SYNTHETIC_CASES, validate
from aj_llm_layer.execution import DisabledAdapter, Request


def test_independent_objective_validation():
    assert validate(SYNTHETIC_CASES[0], "42")
    assert not validate(SYNTHETIC_CASES[0], "41")
    assert validate(SYNTHETIC_CASES[1], '{"label": "positive"}')


def test_bandit_rejects_unvalidated_observations():
    b = Bandit()
    with pytest.raises(ValueError):
        b.observe(Outcome("math", "a", False, True, .01, 20))


def test_bandit_is_task_specific_and_accounts_for_failure_cost():
    b = Bandit()
    b.observe(Outcome("math", "a", True, False, .02, 20))
    b.observe(Outcome("math", "a", True, True, .03, 30))
    assert b.cost_per_success("math", "a") == pytest.approx(.05)
    assert b.quality_lower_bound("coding", "a") == 0


def test_provider_egress_disabled():
    with pytest.raises(PermissionError):
        DisabledAdapter().complete(Request("model", "math", "synthetic"))
