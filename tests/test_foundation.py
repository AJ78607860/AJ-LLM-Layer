from aj_llm_layer.policy import RoutingContext, classify_data, eligible_provider
from aj_llm_layer.ranking import Candidate, rank


def test_caller_cannot_downgrade_personal():
    assert classify_data(RoutingContext("anything", "non_personal", "test")) == "personal"


def test_no_live_egress_enabled():
    assert not eligible_provider("openrouter", "personal")


def test_cheapest_qualified_model_wins():
    rows = (
        Candidate("a", "model-a", .10, .98, 1200),
        Candidate("b", "model-b", .02, .95, 1500),
        Candidate("c", "model-c", .01, .80, 500),
    )
    assert [x.provider for x in rank(rows, min_quality=.90, max_latency_p95_ms=2000)] == ["b", "a"]


def test_unknown_quality_fails_closed():
    assert rank((Candidate("a", "m", .01, .5, 500),), min_quality=.9, max_latency_p95_ms=1000) == ()
