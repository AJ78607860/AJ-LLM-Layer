"""Certification decide/outcome contract. Dummy keys, temp SQLite ledger, no network, no provider calls."""
import json
import threading
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from aj_llm_layer import service_auth
from aj_llm_layer.api import app
from aj_llm_layer.certification import config as cert_config, service
from aj_llm_layer.certification.ledger import BudgetRefused, Ledger, LedgerUnavailable

KEY = "dummy-certification-test-key-0123456789-abcdef"
DECIDE = "/internal/v1/certification/decide"
OUTCOME = "/internal/v1/certification/outcome"
STATUS = "/internal/v1/certification/status"
SCOPES = {DECIDE: "certify:decide", OUTCOME: "certify:outcome", STATUS: "certify:read"}
TASK = {"schema_version": "aj.tasks.v1", "families": ["reasoning"], "input_tokens": 200, "output_tokens": 64,
        "cached_tokens": 0, "context_tokens": 0, "structured": False, "tools": False,
        "modalities": ["text"], "criticality": "normal"}


@pytest.fixture
def cert(monkeypatch, tmp_path):
    monkeypatch.setenv("AJ_ENVIRONMENT", "staging")
    monkeypatch.setenv("AJ_LLM_LAYER_SIGNING_KEYS", "test:" + KEY)
    monkeypatch.setenv("AJ_LLM_LAYER_CERTIFICATION_ENABLED", "true")
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD", "1.00")
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_MAX_RUN_USD", "0.05")
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    for name in ("AJ_LLM_LAYER_CERT_APPROVED_PROVIDERS", "AJ_LLM_LAYER_CERT_MAX_CONCURRENT",
                 "AJ_LLM_LAYER_CERT_GRANT_TTL_SECONDS", "AJ_LLM_LAYER_CERT_REGISTRY_PATH"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _body(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def call(path, payload=None, *, scope=None, method="POST", credential=None, cid="req_certtest0001"):
    body = _body(payload) if payload is not None else b""
    auth = credential or service_auth.sign(method=method, path=path, body=body, scope=scope or SCOPES[path],
                                           key=KEY.encode(), kid="test", env="staging")
    headers = {"X-AJ-Service-Auth": auth, "X-AJ-Correlation-ID": cid, "Content-Type": "application/json"}
    client = TestClient(app)
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, content=body, headers=headers)


def decide_payload(task=None, task_class="general_reasoning", tier="standard"):
    return {"schema": "aj.cert.decide.v1", "fixture_id": "cert_test_v1",
            "cortex": {"task_class": task_class, "tier": tier, "data_class": "personal"}, "task": task or TASK}


def ok_attempt(**kw):
    a = {"input_tokens": 150, "cached_tokens": 0, "output_tokens": 40, "latency_ms": 800, "error": None,
         "usage_known": True}
    a.update(kw)
    return a


def report(grant, status="succeeded", attempts=None, passed=True):
    payload = {"schema": "aj.cert.outcome.v1", "grant_id": grant["grant_id"], "status": status,
               "attempts": attempts if attempts is not None else [ok_attempt()]}
    if status in ("succeeded", "failed"):
        payload["validation"] = {"passed": passed, "validator": "cert.test"}
    return call(OUTCOME, payload)


# --------------------------------------------------------------------------- gates

def test_production_and_unset_environment_expose_nothing(cert, monkeypatch):
    for env in ("production", "prod", ""):
        monkeypatch.setenv("AJ_ENVIRONMENT", env)
        assert call(DECIDE, decide_payload()).status_code == 404
        assert call(OUTCOME, {"x": 1}).status_code == 404
        assert call(STATUS).status_code == 404


def test_disabled_flag_refuses_new_decisions_after_auth(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERTIFICATION_ENABLED", "false")
    response = call(DECIDE, decide_payload())
    assert response.status_code == 503 and response.json()["detail"] == "certification disabled"


def test_signed_credentials_required_and_scoped(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_SHADOW_TOKEN", "x" * 40)
    client = TestClient(app)
    static = client.post(DECIDE, content=_body(decide_payload()), headers={"Authorization": "Bearer " + "x" * 40})
    assert static.status_code == 401
    assert call(DECIDE, decide_payload(), scope="shadow:write").status_code == 401
    assert call(DECIDE, decide_payload(), scope="certify:outcome").status_code == 401


def test_replayed_decide_credential_rejected(cert):
    body = _body(decide_payload())
    credential = service_auth.sign(method="POST", path=DECIDE, body=body, scope="certify:decide",
                                   key=KEY.encode(), kid="test", env="staging")
    assert call(DECIDE, decide_payload(), credential=credential).status_code == 200
    assert call(DECIDE, decide_payload(), credential=credential).status_code == 401


def test_content_fields_rejected(cert):
    bad = decide_payload()
    bad["prompt"] = "hello"
    assert call(DECIDE, bad).status_code == 422
    task = dict(TASK, prompt="hello")
    response = call(DECIDE, decide_payload(task))
    assert response.status_code == 200 and response.json()["grant"] is None
    assert response.json()["refusal"] == "INVALID_TASK_METADATA"


def test_ledger_unconfigured_fails_closed(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_LEDGER_PATH", "")
    assert call(DECIDE, decide_payload()).status_code == 503


def test_ledger_unwritable_fails_closed(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_LEDGER_PATH", str(cert / "missing-dir" / "ledger.sqlite3"))
    response = call(DECIDE, decide_payload())
    assert response.status_code == 503 and response.json()["detail"] == "budget ledger unavailable"


def test_zero_budget_default_never_grants(cert, monkeypatch):
    monkeypatch.delenv("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD")
    record = call(DECIDE, decide_payload()).json()
    assert record["grant"] is None and record["refusal"] == "BUDGET_UNAVAILABLE"


# ------------------------------------------------------------------- happy path

def test_first_decision_is_bootstrap_probe_on_cheapest_approved_model(cert):
    response = call(DECIDE, decide_payload())
    assert response.status_code == 200
    assert response.headers["x-aj-correlation-id"] == "req_certtest0001"
    record = response.json()
    grant = record["grant"]
    assert grant["kind"] == "probe" and grant["model_id"] == "openai/gpt-5.6-luna"
    assert grant["provider"] == "openai" and grant["api_model"] == "gpt-5.6-luna"
    assert grant["max_attempts"] == 1 and grant["max_output_tokens"] == 64
    assert record["decision"]["execution_authorized"] is False
    excluded = record["decision"]["excluded"]
    assert "PROVIDER_NOT_APPROVED" in excluded["groq/openai/gpt-oss-120b"]
    assert record["production_routing_changed"] is False
    # Reserved = (200 * 0.20 + 64 * 1.20) / 1e6
    assert Decimal(grant["reserved"]) == Decimal("0.00011680")
    assert record["budget_after"]["held_count"] == 1


def test_outcome_settles_actual_cost_and_releases_remainder(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    result = report(grant).json()
    assert result["counted_as_evidence"] is True
    # (150 * 0.20 + 40 * 1.20) / 1e6 = 0.000078
    assert Decimal(result["settlement"]["charged"]) == Decimal("0.00007800")
    assert Decimal(result["settlement"]["released"]) == Decimal("0.00003880")
    assert result["settlement"]["overspend"] == "0"
    assert result["budget"]["held_count"] == 0
    second = report(grant)
    assert second.status_code == 409 and second.json()["code"] == "GRANT_ALREADY_CLOSED"


def test_validated_outcomes_graduate_probe_to_evidence_based_route(cert):
    for _ in range(20):
        grant = call(DECIDE, decide_payload()).json()["grant"]
        assert grant["kind"] == "probe"
        assert report(grant).status_code == 200
    record = call(DECIDE, decide_payload()).json()
    assert record["decision"]["status"] == "RECOMMENDATION"
    assert record["grant"]["kind"] == "route" and record["grant"]["model_id"] == "openai/gpt-5.6-luna"
    assert record["grant"]["max_attempts"] == 2
    status = call(STATUS).json()
    assert status["evidence"]["openai/gpt-5.6-luna"]["samples"] == 20
    assert status["evidence"]["openai/gpt-5.6-luna"]["source"] == "synthetic"


def test_refused_by_policy_releases_everything_and_is_not_evidence(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    result = report(grant, status="refused_by_policy", attempts=[]).json()
    assert result["settlement"]["charged"] == "0E-8" or Decimal(result["settlement"]["charged"]) == 0
    assert result["counted_as_evidence"] is False and result["health"] is None


# ---------------------------------------------------------------- failure paths

def test_timeout_charged_at_bound_and_breaker_opens_then_next_model(cert):
    for _ in range(3):
        grant = call(DECIDE, decide_payload()).json()["grant"]
        assert grant["model_id"] == "openai/gpt-5.6-luna"
        result = report(grant, status="failed", passed=False,
                        attempts=[ok_attempt(error="timeout", usage_known=False, output_tokens=0, input_tokens=0)]).json()
        assert "USAGE_UNKNOWN_CHARGED_AT_BOUND" in result["flags"]
        assert Decimal(result["settlement"]["charged"]) == Decimal(grant["reserved"])
    assert result["health"]["circuit_open"] is True
    record = call(DECIDE, decide_payload()).json()
    assert record["circuit_open"] == ["openai/gpt-5.6-luna"]
    assert "MODEL_DISABLED" in record["decision"]["excluded"]["openai/gpt-5.6-luna"]
    assert record["grant"]["model_id"] == "openai/gpt-5.6-terra"


def test_quality_failure_does_not_trip_breaker(cert):
    for _ in range(4):
        grant = call(DECIDE, decide_payload()).json()["grant"]
        result = report(grant, status="failed", passed=False).json()
        assert result["health"]["circuit_open"] is False


def test_bound_violations_are_charged_but_never_evidence(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    result = report(grant, attempts=[ok_attempt(output_tokens=999)]).json()
    assert "OUTPUT_BOUND_EXCEEDED" in result["flags"] and result["counted_as_evidence"] is False


def test_succeeded_requires_passing_validation(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    response = report(grant, passed=False)
    assert response.status_code == 409 and response.json()["code"] == "INCONSISTENT_OUTCOME"


def test_abandoned_grant_is_charged_in_full_and_closed(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_GRANT_TTL_SECONDS", "10")
    grant = call(DECIDE, decide_payload()).json()["grant"]
    real = time.time
    assert grant["expires_at"] - int(real()) >= grant["max_attempts"] * grant["timeout_seconds"] + 30 - 1
    monkeypatch.setattr(time, "time", lambda: real() + 120)
    response = report(grant)
    assert response.status_code == 409 and response.json()["code"] == "GRANT_EXPIRED"
    budget = call(STATUS).json()["budget"]
    assert Decimal(budget["spent"]) == Decimal(grant["reserved"]) and budget["held_count"] == 0


def test_daily_budget_exhaustion_refuses(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD", "0.0002")
    first = call(DECIDE, decide_payload()).json()
    assert first["grant"] is not None
    second = call(DECIDE, decide_payload()).json()
    assert second["grant"] is None and second["refusal"] in ("DAILY_BUDGET_EXHAUSTED", "BUDGET_EXCEEDED")


def test_run_cap_blocks_oversized_task(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_MAX_RUN_USD", "0.00001")
    record = call(DECIDE, decide_payload()).json()
    assert record["grant"] is None


def test_concurrency_limit(cert):
    call(DECIDE, decide_payload())
    call(DECIDE, decide_payload())
    third = call(DECIDE, decide_payload()).json()
    assert third["grant"] is None and third["refusal"] == "CONCURRENCY_LIMIT"


def test_high_stakes_and_finance_are_refused_without_execution(cert):
    high = call(DECIDE, decide_payload(dict(TASK, criticality="high_stakes"), task_class="safety_critical",
                                       tier="strong")).json()
    assert high["grant"] is None and high["refusal"] == "HIGH_STAKES_RESTRICTED"
    fin = call(DECIDE, decide_payload(dict(TASK, families=["financial_analysis"]),
                                      task_class="financial_interpretation")).json()
    assert fin["grant"] is None


def test_unknown_task_refused(cert):
    record = call(DECIDE, decide_payload(dict(TASK, families=["astrology"]))).json()
    assert record["grant"] is None and record["refusal"] == "UNKNOWN_TASK"


def test_other_provider_needs_explicit_approval(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_APPROVED_PROVIDERS", "groq")
    record = call(DECIDE, decide_payload()).json()
    # Groq models are not marked approved in the registry even when the provider is listed.
    assert record["grant"] is None


# ------------------------------------------------------------------ atomicity

def test_concurrent_reservations_never_exceed_daily_cap(tmp_path):
    ledger = Ledger(str(tmp_path / "l.sqlite3"))
    now = int(time.time())
    granted, errors = [], []

    def worker(i):
        grant = {"grant_id": f"grt_{i:032x}", "reservation_id": f"rsv_{i:032x}", "decision_id": "d",
                 "correlation_id": "c", "kind": "probe", "model_id": "m", "provider": "openai", "api_model": "m",
                 "families": ["reasoning"], "max_input_tokens": 1, "max_output_tokens": 1, "max_attempts": 1,
                 "timeout_seconds": 1, "price": {}, "expires_at": now + 60}
        try:
            ledger.reserve_and_grant(grant, amount=Decimal("0.10"), daily_cap=Decimal("0.35"),
                                     max_run=Decimal("1"), max_concurrent=100, now=now)
            granted.append(i)
        except BudgetRefused as refused:
            errors.append(refused.code)
        except LedgerUnavailable:
            errors.append("unavailable")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 3
    assert Decimal(ledger.budget(daily_cap=Decimal("0.35"), now=now)["held"]) == Decimal("0.30")


def test_config_ceilings_cannot_be_raised_by_env(monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD", "10000")
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_MAX_RUN_USD", "nan")
    cfg = cert_config.load()
    assert cfg.daily_budget == cert_config.HARD_MAX_DAILY_USD and cfg.max_run_budget == 0


def test_certification_never_imports_execution_adapter():
    import aj_llm_layer.certification.service as svc
    source = open(svc.__file__).read()
    assert "execution" not in source and "httpx" not in source and "requests" not in source


def test_grant_outlives_all_attempts(cert, monkeypatch):
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_GRANT_TTL_SECONDS", "10")
    grant = call(DECIDE, decide_payload()).json()["grant"]
    assert grant["expires_at"] >= int(time.time()) + grant["max_attempts"] * grant["timeout_seconds"] + 29


# ------------------------------------------------- independent review regressions (19 Oct)

def test_m1_cached_token_estimate_cannot_shrink_the_hold(cert):
    task = dict(TASK, input_tokens=20000, cached_tokens=19000, output_tokens=64)
    grant = call(DECIDE, decide_payload(task)).json()["grant"]
    # Reserved as if nothing were cached: (20000 * 0.20 + 64 * 1.20) / 1e6
    assert Decimal(grant["reserved"]) == Decimal("0.00407680")
    result = report(grant, attempts=[ok_attempt(input_tokens=20000, cached_tokens=0, output_tokens=64)]).json()
    assert result["settlement"]["overspend"] == "0" and result["counted_as_evidence"] is True


def test_m1_overspend_is_flagged_persisted_and_never_evidence(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    result = report(grant, attempts=[ok_attempt(input_tokens=200, output_tokens=64, cached_tokens=0)]).json()
    assert result["settlement"]["overspend"] == "0"
    grant = call(DECIDE, decide_payload()).json()["grant"]
    # Within token bounds but priced above the hold is impossible; force it via a bound breach.
    result = report(grant, attempts=[ok_attempt(input_tokens=5000, output_tokens=64)]).json()
    assert "RESERVATION_EXCEEDED" in result["flags"] and result["counted_as_evidence"] is False
    import sqlite3
    flags = sqlite3.connect(str(cert / "ledger.sqlite3")).execute(
        "SELECT flags FROM outcomes WHERE grant_id=?", (grant["grant_id"],)).fetchone()[0]
    assert "RESERVATION_EXCEEDED" in flags


def test_m4_evidence_is_per_family(cert):
    extraction = dict(TASK, families=["extraction"], structured=True)
    for _ in range(20):
        grant = call(DECIDE, decide_payload(extraction, task_class="simple_extraction")).json()["grant"]
        report(grant)
    assert call(DECIDE, decide_payload(extraction, task_class="simple_extraction")).json()["grant"]["kind"] == "route"
    planning = call(DECIDE, decide_payload(dict(TASK, families=["planning"]))).json()
    # No planning evidence yet: bootstrap probe on the cheapest model, not an evidence-based route.
    assert planning["grant"]["kind"] == "probe" and planning["grant"]["model_id"] == "openai/gpt-5.6-luna"


def test_m5_one_early_failure_does_not_lock_out_the_cheap_model(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    report(grant, status="failed", passed=False)
    for _ in range(19):
        grant = call(DECIDE, decide_payload()).json()["grant"]
        report(grant)
    record = call(DECIDE, decide_payload()).json()
    assert record["grant"]["model_id"] == "openai/gpt-5.6-luna" and record["grant"]["kind"] == "probe"


def test_l1_contract_violations_never_move_the_breaker(cert):
    for _ in range(3):
        grant = call(DECIDE, decide_payload()).json()["grant"]
        result = report(grant, status="failed", passed=False, attempts=[
            ok_attempt(error="timeout", usage_known=False, output_tokens=0, input_tokens=0),
            ok_attempt(error="timeout", usage_known=False, output_tokens=0, input_tokens=0)]).json()
        assert "ATTEMPT_BOUND_EXCEEDED" in result["flags"] and result["health"] is None
    assert call(STATUS).json()["circuit_open"] == []


def test_l10_failed_with_passing_validation_is_inconsistent(cert):
    grant = call(DECIDE, decide_payload()).json()["grant"]
    response = report(grant, status="failed", passed=True)
    assert response.status_code == 409 and response.json()["code"] == "INCONSISTENT_OUTCOME"


def test_l8_broken_registry_fails_closed_with_503(cert, monkeypatch):
    bad = cert / "registry.json"
    bad.write_text("{not json")
    monkeypatch.setenv("AJ_LLM_LAYER_CERT_REGISTRY_PATH", str(bad))
    response = call(DECIDE, decide_payload())
    assert response.status_code == 503 and response.json()["detail"] == "model registry unavailable"


def test_l6_probe_limit_enforced_inside_the_reservation(tmp_path):
    ledger = Ledger(str(tmp_path / "l.sqlite3"))
    now = int(time.time())

    def g(i):
        return {"grant_id": f"grt_{i:032x}", "reservation_id": f"rsv_{i:032x}", "decision_id": "d",
                "correlation_id": "c", "kind": "probe", "model_id": "m", "provider": "openai", "api_model": "m",
                "families": ["reasoning"], "max_input_tokens": 1, "max_output_tokens": 1, "max_attempts": 1,
                "timeout_seconds": 1, "price": {}, "expires_at": now + 60}
    ledger.reserve_and_grant(g(1), amount=Decimal("0.01"), daily_cap=Decimal("1"), max_run=Decimal("1"),
                             max_concurrent=10, now=now, probe_limit=1)
    with pytest.raises(BudgetRefused) as refused:
        ledger.reserve_and_grant(g(2), amount=Decimal("0.01"), daily_cap=Decimal("1"), max_run=Decimal("1"),
                                 max_concurrent=10, now=now, probe_limit=1)
    assert refused.value.code == "PROBE_LIMIT_REACHED"


# ------------------------------------------------------------------ startup diagnostics

def test_diagnostics_are_content_free_and_name_blockers(cert, monkeypatch):
    from aj_llm_layer.certification import diagnostics
    monkeypatch.setenv("AJ_LLM_LAYER_REPLAY_BACKEND", "local")
    monkeypatch.setenv("AJ_ENVIRONMENT", "development")
    snap = diagnostics.snapshot()
    assert snap["ready"] is True and snap["blockers"] == []
    assert snap["signing_key_ids"] == ["test"] and KEY not in json.dumps(snap)
    monkeypatch.setenv("AJ_LLM_LAYER_SIGNING_KEYS", "short:nope")
    monkeypatch.delenv("AJ_LLM_LAYER_CERT_DAILY_BUDGET_USD")
    snap = diagnostics.snapshot()
    assert {"NO_VALID_SIGNING_KEYS", "NO_DAILY_BUDGET"} <= set(snap["blockers"])
    assert snap["signing_entries_rejected"] == 1 and "nope" not in json.dumps(snap)


def test_diagnostics_report_unreachable_redis_without_url(cert, monkeypatch):
    from aj_llm_layer.certification import diagnostics
    monkeypatch.setenv("AJ_LLM_LAYER_REPLAY_BACKEND", "redis")
    monkeypatch.setenv("AJ_LLM_LAYER_REPLAY_REDIS_URL", "rediss://:secretpw@127.0.0.1:1/0")
    snap = diagnostics.snapshot()
    assert snap["replay"] in ("unreachable", "unavailable") and not snap["ready"]
    assert "secretpw" not in json.dumps(snap)


def test_startup_log_in_production_says_nothing_else(monkeypatch, caplog):
    from aj_llm_layer.certification import diagnostics
    monkeypatch.setenv("AJ_ENVIRONMENT", "production")
    with caplog.at_level("INFO", logger="uvicorn.error"):
        diagnostics.log_startup()
    assert json.loads(caplog.records[-1].getMessage()) == {"event": "certification_config",
                                                           "environment_allowed": False}


def test_status_reports_readiness(cert):
    status = call(STATUS).json()
    assert "ready" in status and status["signing_key_ids"] == ["test"]


# ------------------------------------------------------- decide wire contract (pinned; AJ replays these shapes)

def test_decide_contract_pairs_are_pinned(cert):
    """AJ executes only (RECOMMENDATION, route) and (NO_ELIGIBLE_MODEL, probe) with refusal null, and checks a
    probe's consistency. If this test must change, re-record AJ's tests/fixtures/llm_layer_contract/ fixtures."""
    probe = call(DECIDE, decide_payload()).json()
    d, g = probe["decision"], probe["grant"]
    assert (d["status"], g["kind"], probe["refusal"]) == ("NO_ELIGIBLE_MODEL", "probe", None)
    assert d["selected"] is None and d["reasons"] == ["NO_ELIGIBLE_MODEL"] and g["max_attempts"] == 1
    assert set(d["excluded"][g["model_id"]]) <= {"INSUFFICIENT_EVIDENCE", "QUALITY_BELOW_THRESHOLD",
                                                 "RELIABILITY_BELOW_THRESHOLD"}
    assert g["model_id"] == f'{g["provider"]}/{g["api_model"]}'
    report(g)
    for _ in range(19):
        report(call(DECIDE, decide_payload()).json()["grant"])
    route = call(DECIDE, decide_payload()).json()
    assert (route["decision"]["status"], route["grant"]["kind"], route["refusal"]) == ("RECOMMENDATION", "route", None)
    assert route["decision"]["selected"] == route["grant"]["model_id"]
    refused = call(DECIDE, decide_payload(dict(TASK, criticality="high_stakes"), task_class="safety_critical")).json()
    assert refused["grant"] is None and refused["refusal"]


def test_diagnostics_fast_and_secret_free_with_redis_down(cert, monkeypatch):
    import time as _t
    from aj_llm_layer.certification import diagnostics
    monkeypatch.setenv("AJ_LLM_LAYER_REPLAY_BACKEND", "redis")
    monkeypatch.setenv("AJ_LLM_LAYER_REPLAY_REDIS_URL", "rediss://:pw-should-not-leak@10.255.255.1:6379/0")
    started = _t.monotonic()
    snap = diagnostics.snapshot()
    assert _t.monotonic() - started < 3 and snap["replay"] in ("unreachable", "unavailable")
    assert "pw-should-not-leak" not in json.dumps(snap)


def test_lifespan_wrapper_keeps_app_working_and_logs_once(monkeypatch, caplog):
    monkeypatch.setenv("AJ_ENVIRONMENT", "production")
    with caplog.at_level("INFO", logger="uvicorn.error"):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    lines = [r.getMessage() for r in caplog.records if "certification_config" in r.getMessage()]
    assert len(lines) == 1
