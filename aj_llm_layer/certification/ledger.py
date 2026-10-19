"""Atomic certification budget ledger, grants, outcomes and model health (SQLite).

One writer at a time (``BEGIN IMMEDIATE``), so concurrent ``decide`` calls can
never jointly overspend the daily cap. Amounts are Decimal strings. Any storage
error raises ``LedgerUnavailable``; callers must then refuse new billable work
(founder decision 8: ledger outage fails closed).

Rows hold metadata only: ids, model ids, task families, token counts, latency,
charges and fixed reason codes. Never prompts, responses or user identifiers.

Limitation: SQLite on an ephemeral disk resets on restart. AJ's own cost caps
(core.cost.guard) remain authoritative; a durable ledger is a separate decision.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (
  id TEXT PRIMARY KEY, day TEXT NOT NULL, amount TEXT NOT NULL, charged TEXT,
  status TEXT NOT NULL CHECK (status IN ('held','settled','released','abandoned')),
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, closed_at INTEGER);
CREATE INDEX IF NOT EXISTS reservations_day ON reservations(day, status);
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE, decision_id TEXT NOT NULL,
  correlation_id TEXT NOT NULL, kind TEXT NOT NULL, model_id TEXT NOT NULL, provider TEXT NOT NULL,
  api_model TEXT NOT NULL, families TEXT NOT NULL, max_input_tokens INTEGER NOT NULL,
  max_output_tokens INTEGER NOT NULL, max_attempts INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL,
  price TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('issued','completed','expired')),
  issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outcomes (
  grant_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, families TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL, validated INTEGER NOT NULL, transport_failures INTEGER NOT NULL,
  timeouts INTEGER NOT NULL, charged TEXT NOT NULL, counted INTEGER NOT NULL, flags TEXT NOT NULL,
  recorded_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS outcomes_model ON outcomes(model_id, recorded_at);
CREATE TABLE IF NOT EXISTS health (
  model_id TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL, open_until INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT NOT NULL, correlation_id TEXT NOT NULL, recorded_at INTEGER NOT NULL,
  record TEXT NOT NULL);
"""


class LedgerUnavailable(Exception):
    """Fixed public error; storage details are never propagated."""


class BudgetRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GrantRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def day_key(now: int) -> str:
    return datetime.fromtimestamp(now, timezone.utc).astimezone(LONDON).strftime("%Y-%m-%d")


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class Ledger:
    def __init__(self, path: str):
        if not path:
            raise LedgerUnavailable()
        self.path = path
        try:
            db = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                # executescript commits on its own; statements are idempotent.
                db.executescript(SCHEMA + "INSERT OR IGNORE INTO meta(k, v) VALUES ('epoch', strftime('%s','now'));")
            finally:
                db.close()
        except Exception:
            raise LedgerUnavailable() from None

    @contextmanager
    def _tx(self, immediate: bool = False):
        try:
            db = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        except Exception:
            raise LedgerUnavailable() from None
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield db
            db.execute("COMMIT")
        except (BudgetRefused, GrantRejected):
            db.execute("ROLLBACK")
            raise
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise LedgerUnavailable() from None
        finally:
            db.close()

    # ------------------------------------------------------------------ budget
    @staticmethod
    def _totals(db, day: str) -> tuple[Decimal, Decimal, int]:
        spent = held = Decimal("0")
        count = 0
        for status, amount, charged in db.execute(
                "SELECT status, amount, charged FROM reservations WHERE day = ?", (day,)):
            if status == "held":
                held += Decimal(amount)
                count += 1
            elif status in ("settled", "abandoned"):
                spent += Decimal(charged or "0")
        return spent, held, count

    def budget(self, *, daily_cap: Decimal, now: int) -> dict:
        day = day_key(now)
        with self._tx() as db:
            spent, held, count = self._totals(db, day)
            epoch = db.execute("SELECT v FROM meta WHERE k='epoch'").fetchone()[0]
        remaining = max(Decimal("0"), daily_cap - spent - held)
        return {"day": day, "currency": "USD", "daily_cap": str(daily_cap), "spent": str(spent),
                "held": str(held), "held_count": count, "remaining": str(remaining), "ledger_epoch": int(epoch)}

    def sweep(self, *, now: int) -> list[str]:
        """Expired, unreported grants are charged their full reservation (conservative)."""
        abandoned = []
        with self._tx(immediate=True) as db:
            rows = db.execute(
                "SELECT g.id, r.id, r.amount FROM grants g JOIN reservations r ON r.id = g.reservation_id "
                "WHERE g.status = 'issued' AND r.status = 'held' AND g.expires_at <= ?", (now,)).fetchall()
            for grant_id, reservation_id, amount in rows:
                db.execute("UPDATE reservations SET status='abandoned', charged=?, closed_at=? WHERE id=?",
                           (amount, now, reservation_id))
                db.execute("UPDATE grants SET status='expired' WHERE id=?", (grant_id,))
                abandoned.append(grant_id)
        return abandoned

    def reserve_and_grant(self, grant: dict, *, amount: Decimal, daily_cap: Decimal, max_run: Decimal,
                          max_concurrent: int, now: int) -> None:
        if amount <= 0:
            raise BudgetRefused("INVALID_RESERVATION")
        if amount > max_run:
            raise BudgetRefused("RUN_BUDGET_EXCEEDED")
        day = day_key(now)
        with self._tx(immediate=True) as db:
            spent, held, count = self._totals(db, day)
            if count >= max_concurrent:
                raise BudgetRefused("CONCURRENCY_LIMIT")
            if spent + held + amount > daily_cap:
                raise BudgetRefused("DAILY_BUDGET_EXHAUSTED")
            db.execute("INSERT INTO reservations(id, day, amount, status, created_at, expires_at) "
                       "VALUES (?, ?, ?, 'held', ?, ?)",
                       (grant["reservation_id"], day, str(amount), now, grant["expires_at"]))
            db.execute(
                "INSERT INTO grants(id, reservation_id, decision_id, correlation_id, kind, model_id, provider, "
                "api_model, families, max_input_tokens, max_output_tokens, max_attempts, timeout_seconds, price, "
                "status, issued_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?)",
                (grant["grant_id"], grant["reservation_id"], grant["decision_id"], grant["correlation_id"],
                 grant["kind"], grant["model_id"], grant["provider"], grant["api_model"],
                 json.dumps(sorted(grant["families"])), grant["max_input_tokens"], grant["max_output_tokens"],
                 grant["max_attempts"], grant["timeout_seconds"], json.dumps(grant["price"], sort_keys=True),
                 now, grant["expires_at"]))

    # ------------------------------------------------------------------ grants
    def grant(self, grant_id: str) -> dict | None:
        with self._tx() as db:
            row = db.execute(
                "SELECT g.id, g.reservation_id, g.decision_id, g.correlation_id, g.kind, g.model_id, g.provider, "
                "g.api_model, g.families, g.max_input_tokens, g.max_output_tokens, g.max_attempts, "
                "g.timeout_seconds, g.price, g.status, g.issued_at, g.expires_at, r.amount, r.status "
                "FROM grants g JOIN reservations r ON r.id = g.reservation_id WHERE g.id = ?",
                (grant_id,)).fetchone()
        if row is None:
            return None
        keys = ("grant_id", "reservation_id", "decision_id", "correlation_id", "kind", "model_id", "provider",
                "api_model", "families", "max_input_tokens", "max_output_tokens", "max_attempts",
                "timeout_seconds", "price", "status", "issued_at", "expires_at", "reserved", "reservation_status")
        data = dict(zip(keys, row))
        data["families"] = json.loads(data["families"])
        data["price"] = json.loads(data["price"])
        return data

    def settle(self, grant_id: str, *, charged: Decimal, outcome: dict, now: int) -> dict:
        """Charge the actual cost once; release the remainder. Single use per grant."""
        with self._tx(immediate=True) as db:
            row = db.execute(
                "SELECT g.status, g.reservation_id, r.status, r.amount FROM grants g "
                "JOIN reservations r ON r.id = g.reservation_id WHERE g.id = ?", (grant_id,)).fetchone()
            if row is None:
                raise GrantRejected("UNKNOWN_GRANT")
            grant_status, reservation_id, reservation_status, amount = row
            if grant_status != "issued" or reservation_status != "held":
                raise GrantRejected("GRANT_ALREADY_CLOSED")
            reserved = Decimal(amount)
            status = "released" if charged == 0 else "settled"
            db.execute("UPDATE reservations SET status=?, charged=?, closed_at=? WHERE id=?",
                       (status, str(charged), now, reservation_id))
            db.execute("UPDATE grants SET status='completed' WHERE id=?", (grant_id,))
            db.execute(
                "INSERT INTO outcomes(grant_id, model_id, families, status, attempts, input_tokens, output_tokens, "
                "latency_ms, validated, transport_failures, timeouts, charged, counted, flags, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (grant_id, outcome["model_id"], json.dumps(sorted(outcome["families"])), outcome["status"],
                 outcome["attempts"], outcome["input_tokens"], outcome["output_tokens"], outcome["latency_ms"],
                 1 if outcome["validated"] else 0, outcome["transport_failures"], outcome["timeouts"],
                 str(charged), 1 if outcome["counted"] else 0, json.dumps(sorted(outcome["flags"])), now))
        return {"reserved": str(reserved), "charged": str(charged),
                "released": str(max(Decimal("0"), reserved - charged)),
                "overspend": str(max(Decimal("0"), charged - reserved))}

    # ---------------------------------------------------------------- evidence
    def evidence(self, model_id: str, *, since: int) -> dict | None:
        with self._tx() as db:
            rows = db.execute(
                "SELECT families, attempts, validated, transport_failures, timeouts, latency_ms, charged, "
                "recorded_at FROM outcomes WHERE model_id=? AND counted=1 AND recorded_at>=?",
                (model_id, since)).fetchall()
        if not rows:
            return None
        families: set[str] = set()
        attempts = successes = failures = timeouts = 0
        latencies: list[int] = []
        cost = Decimal("0")
        observed = 0
        for fams, att, validated, tf, to, latency, charged, recorded in rows:
            families.update(json.loads(fams))
            attempts += att
            successes += validated
            failures += tf
            timeouts += to
            latencies.append(latency)
            cost += Decimal(charged)
            observed = max(observed, recorded)
        return {"samples": len(rows), "successes": successes, "attempts": attempts,
                "failure_rate": (failures - timeouts) / attempts if attempts else 0.0,
                "timeout_rate": timeouts / attempts if attempts else 0.0,
                "latency_p95_ms": max(1, _p95(latencies)), "measured_cost": cost,
                "families": sorted(families), "observed_at": observed}

    def probes_today(self, model_id: str, *, now: int) -> int:
        day = day_key(now)
        with self._tx() as db:
            return db.execute(
                "SELECT COUNT(*) FROM grants g JOIN reservations r ON r.id=g.reservation_id "
                "WHERE g.model_id=? AND g.kind='probe' AND r.day=?", (model_id, day)).fetchone()[0]

    # ------------------------------------------------------------------ health
    def open_circuits(self, *, now: int) -> set[str]:
        with self._tx() as db:
            return {m for (m,) in db.execute("SELECT model_id FROM health WHERE open_until > ?", (now,))}

    def record_health(self, model_id: str, *, transport_failure: bool, threshold: int, cooldown: int,
                      now: int) -> dict:
        with self._tx(immediate=True) as db:
            row = db.execute("SELECT consecutive_failures, open_until FROM health WHERE model_id=?",
                             (model_id,)).fetchone()
            failures, open_until = row if row else (0, 0)
            if transport_failure:
                failures += 1
                if failures >= threshold:
                    open_until, failures = now + cooldown, 0
            else:
                failures = 0
            db.execute("INSERT INTO health(model_id, consecutive_failures, open_until) VALUES (?,?,?) "
                       "ON CONFLICT(model_id) DO UPDATE SET consecutive_failures=excluded.consecutive_failures, "
                       "open_until=excluded.open_until", (model_id, failures, open_until))
        return {"model_id": model_id, "consecutive_failures": failures, "circuit_open": open_until > now,
                "open_until": open_until}

    def health(self, *, now: int) -> list[dict]:
        with self._tx() as db:
            return [{"model_id": m, "consecutive_failures": f, "circuit_open": u > now, "open_until": u}
                    for m, f, u in db.execute("SELECT model_id, consecutive_failures, open_until FROM health "
                                              "ORDER BY model_id")]

    # --------------------------------------------------------------- decisions
    def record_decision(self, decision_id: str, correlation_id: str, record: dict, *, now: int) -> None:
        with self._tx(immediate=True) as db:
            db.execute("INSERT INTO decisions(decision_id, correlation_id, recorded_at, record) VALUES (?,?,?,?)",
                       (decision_id, correlation_id, now, json.dumps(record, sort_keys=True, default=str)))
