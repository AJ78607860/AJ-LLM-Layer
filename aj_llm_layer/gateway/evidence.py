"""Evidence ledger: which model actually gives correct answers, per kind of task.

Two kinds of evidence, kept per (task family, model):

* **Measured automatically on every call:** attempts, provider failures, timeouts,
  latency (last 500 calls, for p95) and real cost.
* **Graded correctness:** how many answers were checked and how many were right.
  Grades come from `POST /v1/feedback` (an app's validator, a user's thumbs up or
  down, or an evaluation run) and from objective checks the Layer can do itself
  (an answer that was asked to be JSON but isn't valid JSON is graded wrong).

The routing engine turns this into a quality lower bound (Wilson 95%) and a cost
per *correct* answer, so a model must earn its place with evidence, not claims.
No prompts or answers are stored: only counts, timings and costs.
"""
from __future__ import annotations

import json
import secrets
import time
from decimal import Decimal
from math import ceil
from typing import Callable

from ..routing.contracts import Evidence
from ..routing.eligibility import quality_lower
from .health import Store

LATENCY_WINDOW = 500
CALL_TTL = 7 * 86400


def _p95(samples: list[float]) -> int:
    if not samples:
        return 1
    ordered = sorted(samples)
    return max(1, int(ordered[min(len(ordered) - 1, ceil(0.95 * len(ordered)) - 1)]))


class EvidenceLedger:
    def __init__(self, store: Store, *, catalog_version: str, clock: Callable[[], float] = time.time):
        self.store = store
        self.catalog_version = catalog_version
        self._clock = clock

    @staticmethod
    def _key(family: str, model: str) -> str:
        return f"ev:{family}:{model}"

    # -- automatic measurement -------------------------------------------------
    def record_attempt(self, family: str, model: str, *, ok: bool, timeout: bool,
                       latency_ms: int, cost_usd: float | None) -> None:
        key = self._key(family, model)
        self.store.hincr(key, "calls", 1)
        if not ok:
            self.store.hincr(key, "timeouts" if timeout else "failures", 1)
        else:
            self.store.push_capped(f"lat:{family}:{model}", float(latency_ms), LATENCY_WINDOW)
        if cost_usd:
            self.store.hincr(key, "cost", cost_usd)

    def new_call(self, *, family: str, model: str, provider: str, app: str) -> str:
        call_id = "call_" + secrets.token_hex(12)
        self.store.put(f"call:{call_id}", json.dumps(
            {"family": family, "model": model, "provider": provider, "app": app}), CALL_TTL)
        return call_id

    # -- grading ---------------------------------------------------------------
    def grade(self, family: str, model: str, correct: bool) -> None:
        key = self._key(family, model)
        self.store.hincr(key, "graded", 1)
        if correct:
            self.store.hincr(key, "correct", 1)
        self.store.put(f"lastgrade:{family}:{model}", str(int(self._clock())), 180 * 86400)

    def grade_call(self, call_id: str, correct: bool, *, app: str) -> dict:
        """Grade a previous call once. Only the app that made the call may grade it."""
        raw = self.store.fetch(f"call:{call_id}")
        if raw is None:
            raise KeyError("unknown or expired call")
        call = json.loads(raw)
        if call.get("app") != app:
            raise PermissionError("call belongs to another app")
        if self.store.fetch(f"graded:{call_id}") is not None:
            raise ValueError("call already graded")
        self.store.put(f"graded:{call_id}", "1", CALL_TTL)
        self.grade(call["family"], call["model"], correct)
        return call

    # -- read side -------------------------------------------------------------
    def evidence(self, family: str, model: str) -> Evidence | None:
        h = self.store.hgetall(self._key(family, model))
        graded, correct = int(h.get("graded", 0)), int(h.get("correct", 0))
        calls = max(int(h.get("calls", 0)), 1)
        if graded == 0:
            return None
        return Evidence(
            version=f"ledger-{family}", model_version=self.catalog_version,
            families=frozenset({family}), source="independent", samples=graded,
            successes=min(correct, graded),
            observed_at=int(self.store.fetch(f"lastgrade:{family}:{model}") or 0),
            latency_p95_ms=_p95(self.store.get_list(f"lat:{family}:{model}")),
            failure_rate=min(1.0, h.get("failures", 0) / calls),
            timeout_rate=min(1.0, h.get("timeouts", 0) / calls),
            measured_cost=Decimal(str(round(h.get("cost", 0.0), 8))), currency="USD")

    def summary(self, family: str, model: str) -> dict:
        h = self.store.hgetall(self._key(family, model))
        graded, correct, calls = int(h.get("graded", 0)), int(h.get("correct", 0)), int(h.get("calls", 0))
        ev = self.evidence(family, model)
        cost = h.get("cost", 0.0)
        return {"calls": calls, "graded": graded, "correct": correct,
                "accuracy": round(correct / graded, 4) if graded else None,
                "accuracy_lower_bound": round(quality_lower(_as_model(ev)), 4) if ev else None,
                "failure_rate": round((h.get("failures", 0) + h.get("timeouts", 0)) / calls, 4) if calls else None,
                "latency_p95_ms": _p95(self.store.get_list(f"lat:{family}:{model}")) if calls else None,
                "cost_usd": round(cost, 6),
                "cost_per_correct_usd": round(cost * (graded / calls) / correct, 6) if correct and calls else None}

    def families(self) -> list[str]:
        return sorted({k.split(":")[1] for k in self.store.keys("ev:")})


class _EvidenceOnly:
    def __init__(self, evidence):
        self.evidence = evidence


def _as_model(evidence: Evidence):
    return _EvidenceOnly(evidence)
