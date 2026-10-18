import pytest
from aj_llm_layer.routing.policies import Policy, PolicyCatalog, tighten

def test_downgrade_and_rollback():
    p = Policy(version='p1', approved=True, enabled=True)
    with pytest.raises(ValueError):
        tighten(p, Policy(version='p2', approved=True, enabled=True, min_quality=.1))
    assert PolicyCatalog(policies=(p,), active_version='p1').active() == p
    with pytest.raises(ValueError):
        PolicyCatalog(policies=(p,), active_version='missing').active()
