"""Content-free certification readiness, logged once at startup.

Lets an operator (or a reviewer reading Render logs) confirm configuration without ever
seeing a secret: only booleans, counts, key IDs (never key material), fixed status words
and budget figures are reported. Never raises; diagnostics must not stop the service.
"""
from __future__ import annotations

import json
import logging
import os

from .. import service_auth
from . import config as cert_config

# uvicorn configures its own logger; application loggers at INFO are otherwise dropped.
_log = logging.getLogger("uvicorn.error")


def _replay_status(environment: str) -> str:
    backend = (os.getenv("AJ_LLM_LAYER_REPLAY_BACKEND") or "redis").strip()
    try:
        from ..replay import get_replay_store, RedisReplayStore
        store = get_replay_store()
    except Exception:
        return "unavailable"
    if isinstance(store, RedisReplayStore):
        try:
            return "ok" if store.client.ping() else "unreachable"
        except Exception:
            return "unreachable"
    return "local" if backend == "local" else "unavailable"


def _ledger_status(path: str) -> str:
    if not path:
        return "unconfigured"
    try:
        from .ledger import Ledger
        Ledger(path)
        return "ok"
    except Exception:
        return "unavailable"


def snapshot() -> dict:
    cfg = cert_config.load()
    keys = service_auth.parse_keys(os.getenv("AJ_LLM_LAYER_SIGNING_KEYS", ""))
    raw_entries = [e for e in (os.getenv("AJ_LLM_LAYER_SIGNING_KEYS") or "").split(",") if e.strip()]
    data = {
        "event": "certification_config",
        "environment": cfg.environment or "unset",
        "environment_allowed": cfg.environment_allowed,
        "enabled": cfg.enabled,
        "signing_key_ids": sorted(keys),
        "signing_entries_rejected": max(0, len(raw_entries) - len(keys)),
        "replay": _replay_status(cfg.environment),
        "ledger": _ledger_status(cfg.ledger_path),
        "daily_budget_usd": str(cfg.daily_budget),
        "max_run_usd": str(cfg.max_run_budget),
        "approved_providers": sorted(cfg.approved_providers),
    }
    blockers = []
    if not cfg.environment_allowed:
        blockers.append("ENVIRONMENT_NOT_ALLOWED")
    if not cfg.enabled:
        blockers.append("CERTIFICATION_DISABLED")
    if not keys:
        blockers.append("NO_VALID_SIGNING_KEYS")
    if data["replay"] not in ("ok", "local"):
        blockers.append("REPLAY_STORE_" + data["replay"].upper())
    if data["ledger"] != "ok":
        blockers.append("LEDGER_" + data["ledger"].upper())
    if cfg.daily_budget <= 0:
        blockers.append("NO_DAILY_BUDGET")
    data["ready"] = not blockers
    data["blockers"] = blockers
    return data


def log_startup() -> None:
    try:
        if not cert_config.load().environment_allowed:
            # Production/unknown: certification is unreachable (404); say only that.
            _log.info(json.dumps({"event": "certification_config", "environment_allowed": False}))
            return
        _log.info(json.dumps(snapshot(), sort_keys=True))
    except Exception:
        _log.info(json.dumps({"event": "certification_config", "ready": False, "blockers": ["DIAGNOSTICS_FAILED"]}))
