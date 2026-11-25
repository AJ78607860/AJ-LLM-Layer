"""HTTP surface for the gateway, so any app can call it over the network.

Off unless AJ_LLM_GATEWAY_ENABLED=true (returns 404 otherwise). Each app gets
its own key: AJ_LLM_GATEWAY_KEYS="aj_assistant:<key>,calibre:<key>" (keys at
least 32 characters). The app name from the key is what appears in logs.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .errors import ErrorKind, GatewayError
from .router import Gateway
from .types import CompletionRequest

router = APIRouter()
log = logging.getLogger("aj.llm_layer.gateway.http")
MAX_BODY_BYTES = 2_000_000
MIN_KEY_CHARS = 32

_gateway: Gateway | None = None
_lock = threading.Lock()


def get_gateway() -> Gateway:
    global _gateway
    with _lock:
        if _gateway is None:
            _gateway = Gateway()
        return _gateway


def set_gateway(gateway: Gateway | None) -> None:
    """Test hook and hot-reload hook."""
    global _gateway
    with _lock:
        _gateway = gateway


def _enabled() -> bool:
    return (os.getenv("AJ_LLM_GATEWAY_ENABLED") or "").strip().lower() == "true"


def _app_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for entry in (os.getenv("AJ_LLM_GATEWAY_KEYS") or "").split(","):
        app, sep, key = entry.strip().partition(":")
        if not entry.strip():
            continue
        if not (sep and app and len(key) >= MIN_KEY_CHARS and key.isascii()
                and not any(c.isspace() for c in key)):
            return {}  # malformed configuration fails closed
        keys[app] = key
    return keys


def _authenticate(authorization: str | None) -> str:
    if not _enabled():
        raise HTTPException(status_code=404, detail="not found")
    keys = _app_keys()
    if not keys:
        raise HTTPException(status_code=503, detail="gateway authentication not configured")
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="unauthorized")
    supplied = authorization[7:]
    app = None
    for name, key in keys.items():  # check every key so timing doesn't reveal which matched
        if secrets.compare_digest(supplied.encode(), key.encode()):
            app = name
    if app is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return app


_STATUS = {ErrorKind.BAD_REQUEST: 400, ErrorKind.REFUSED: 422, ErrorKind.NOT_CONFIGURED: 503,
           ErrorKind.QUOTA: 503, ErrorKind.AUTH: 503, ErrorKind.RATE_LIMIT: 429,
           ErrorKind.TIMEOUT: 504, ErrorKind.UNAVAILABLE: 502}


@router.post("/v1/complete")
async def complete(request: Request, authorization: str | None = Header(default=None)):
    app = _authenticate(authorization)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request too large")
    try:
        parsed = CompletionRequest.model_validate_json(bytes(body))
    except ValidationError as exc:
        # Field locations and messages only, never input values.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "invalid_request", "details": errors})
    try:
        result = await run_in_threadpool(get_gateway().complete, parsed, app=app)
    except GatewayError as exc:
        return JSONResponse(status_code=_STATUS.get(exc.kind, 502), content={
            "error": exc.kind.value, "message": str(exc),
            "attempts": [a.model_dump() for a in exc.attempts]})
    return result.model_dump()


@router.get("/v1/status")
def status(authorization: str | None = Header(default=None)):
    _authenticate(authorization)
    return {"providers": get_gateway().status()}
