"""Signed internal certification routes. Signed credentials only; never static bearer tokens."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .. import service_auth
from .. import shadow as _shadow
from ..replay import ReplayUnavailable
from . import config as cert_config
from . import service
from .ledger import GrantRejected, Ledger, LedgerUnavailable

router = APIRouter()
log = logging.getLogger("aj.llm_layer.certification")
DECIDE_PATH = "/internal/v1/certification/decide"
OUTCOME_PATH = "/internal/v1/certification/outcome"
STATUS_PATH = "/internal/v1/certification/status"
SCOPES = {DECIDE_PATH: "certify:decide", OUTCOME_PATH: "certify:outcome", STATUS_PATH: "certify:read"}
MAX_BODY_BYTES = 4096
BODY_TIMEOUT_SECONDS = 2.0


def _gate() -> cert_config.CertConfig:
    cfg = cert_config.load()
    if not cfg.environment_allowed:
        # Production (or unknown) environments expose nothing.
        raise HTTPException(status_code=404, detail="Not Found")
    return cfg


def _ledger(cfg: cert_config.CertConfig) -> Ledger:
    try:
        return Ledger(cfg.ledger_path)
    except LedgerUnavailable:
        raise HTTPException(status_code=503, detail="budget ledger unavailable") from None


def _authenticate(credential: str | None, *, method: str, path: str, body: bytes) -> None:
    keys = service_auth.parse_keys(os.getenv("AJ_LLM_LAYER_SIGNING_KEYS", ""))
    env = (os.getenv("AJ_ENVIRONMENT") or "").strip().lower()
    if not keys or not env:
        raise HTTPException(status_code=503, detail="certification authentication not configured")
    if not credential or not credential.isascii() or len(credential) > service_auth.MAX_HEADER_CHARS:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        service_auth.verify(credential, method=method, path=path, body=body, keys=keys, env=env,
                            scope=SCOPES[path], replay=_shadow.get_replay_store())
    except ReplayUnavailable:
        raise HTTPException(status_code=503, detail="certification authentication unavailable") from None
    except service_auth.InvalidCredential as exc:
        log.warning("certification_auth_rejected reason=%s", exc.reason)
        raise HTTPException(status_code=401, detail="unauthorized") from None


async def _read_body(request: Request) -> bytes:
    chunks = bytearray()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="certification metadata too large")
                chunks.extend(chunk)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="certification metadata timeout") from None
    return bytes(chunks)


def _strict_json(body: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    data = json.loads(body, object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise ValueError("object required")
    return data


def _reject_ambiguous(request: Request, path: str) -> None:
    names = [name.lower() for name, _ in request.scope["headers"]]
    if (any(names.count(h) > 1 for h in (b"x-aj-service-auth", b"x-aj-correlation-id", b"content-length"))
            or request.url.query or request.scope.get("raw_path", path.encode()) != path.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post(DECIDE_PATH)
async def decide_route(request: Request, x_aj_service_auth: str | None = Header(default=None),
                       x_aj_correlation_id: str | None = Header(default=None)):
    cfg = _gate()
    _reject_ambiguous(request, DECIDE_PATH)
    cid = _shadow.correlation_id(x_aj_correlation_id)
    body = await _read_body(request)
    await run_in_threadpool(_authenticate, x_aj_service_auth, method="POST", path=DECIDE_PATH, body=body)
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail="certification disabled")
    try:
        _strict_json(body)
        req = service.DecideRequest.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="invalid certification metadata") from None
    ledger = _ledger(cfg)
    try:
        record = await run_in_threadpool(service.decide, req, cfg=cfg, ledger=ledger, correlation_id=cid,
                                         now=int(time.time()))
    except LedgerUnavailable:
        raise HTTPException(status_code=503, detail="budget ledger unavailable") from None
    log.info("certification_decide correlation_id=%s granted=%s refusal=%s", cid,
             record["grant"] is not None, record["refusal"] or "-")
    return JSONResponse(record, headers={_shadow.CORRELATION_HEADER: cid})


@router.post(OUTCOME_PATH)
async def outcome_route(request: Request, x_aj_service_auth: str | None = Header(default=None),
                        x_aj_correlation_id: str | None = Header(default=None)):
    cfg = _gate()
    _reject_ambiguous(request, OUTCOME_PATH)
    cid = _shadow.correlation_id(x_aj_correlation_id)
    body = await _read_body(request)
    await run_in_threadpool(_authenticate, x_aj_service_auth, method="POST", path=OUTCOME_PATH, body=body)
    # Outcomes are accepted even when certification is disabled, so in-flight grants still settle.
    try:
        _strict_json(body)
        req = service.OutcomeRequest.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="invalid certification outcome") from None
    ledger = _ledger(cfg)
    try:
        result = await run_in_threadpool(service.outcome, req, cfg=cfg, ledger=ledger, now=int(time.time()))
    except GrantRejected as rejected:
        return JSONResponse({"detail": "outcome rejected", "code": rejected.code}, status_code=409,
                            headers={_shadow.CORRELATION_HEADER: cid})
    except LedgerUnavailable:
        raise HTTPException(status_code=503, detail="budget ledger unavailable") from None
    log.info("certification_outcome correlation_id=%s status=%s counted=%s", cid, result["status"],
             result["counted_as_evidence"])
    return JSONResponse(result, headers={_shadow.CORRELATION_HEADER: cid})


@router.get(STATUS_PATH)
async def status_route(request: Request, x_aj_service_auth: str | None = Header(default=None)):
    cfg = _gate()
    _reject_ambiguous(request, STATUS_PATH)
    body = await _read_body(request)
    await run_in_threadpool(_authenticate, x_aj_service_auth, method="GET", path=STATUS_PATH, body=body)
    ledger = _ledger(cfg)
    try:
        return JSONResponse(await run_in_threadpool(service.status, cfg=cfg, ledger=ledger, now=int(time.time())))
    except LedgerUnavailable:
        raise HTTPException(status_code=503, detail="budget ledger unavailable") from None
