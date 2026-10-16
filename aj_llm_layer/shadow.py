"""Metadata-only shadow receiver: signed authentication and safe correlation IDs.

No model execution, user content, or provider calls. Receiver auth defaults to
static for compatibility; staging must explicitly select signed after testing.
ReplayCache is process-local: multiworker/replica production needs shared atomic replay storage.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import asyncio
import json
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import service_auth
from .replay import get_replay_store, ReplayUnavailable
from .policy import RoutingContext, classify_data

router = APIRouter()
log = logging.getLogger("aj.llm_layer.shadow")
CORRELATION_HEADER = "X-AJ-Correlation-ID"
_CORRELATION_RE = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")
MAX_BODY_BYTES = 1024
BODY_TIMEOUT_SECONDS = 2.0


class ShadowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_family: Literal['email_classification', 'order_extraction', 'memory_extraction',
                         'light_text', 'transcription', 'video_transcription', 'browser_agent',
                         'cortex_analysis', 'llm_tools', 'public_web_synthesis', 'public_fact_synthesis']
    estimated_input_tokens: int = Field(ge=0, le=200000, strict=True)
    estimated_output_tokens: int = Field(ge=0, le=200000, strict=True)


def correlation_id(value: str | None) -> str:
    """Use only safe caller IDs; otherwise mint a new unguessable ID."""
    if isinstance(value, str) and _CORRELATION_RE.fullmatch(value):
        return value
    return "llm_" + secrets.token_hex(16)


def _authenticate(*, authorization: str | None, credential: str | None, body: bytes) -> None:
    mode = (os.getenv("AJ_LLM_LAYER_AUTH_MODE") or "static").strip().lower()
    if mode not in ("static", "both", "signed"):
        raise HTTPException(status_code=503, detail="shadow authentication not configured")

    if mode in ("signed", "both") and (credential is not None or mode == "signed"):
        keys = service_auth.parse_keys(os.getenv("AJ_LLM_LAYER_SIGNING_KEYS", ""))
        env = (os.getenv("AJ_ENVIRONMENT") or "").strip().lower()
        if not keys or not env:
            raise HTTPException(status_code=503, detail="shadow authentication not configured")
        try:
            service_auth.verify(
                credential or "", method="POST", path="/internal/v1/shadow",
                body=body, keys=keys, env=env, scope="shadow:write", replay=get_replay_store(),
            )
        except ReplayUnavailable:
            raise HTTPException(status_code=503, detail="shadow authentication unavailable") from None
        except service_auth.InvalidCredential as exc:
            # Safe fixed reason only; never log signed tokens or request bodies.
            log.warning("shadow_auth_rejected reason=%s", exc.reason)
            raise HTTPException(status_code=401, detail="unauthorized") from None
        return

    expected = os.getenv("AJ_LLM_LAYER_SHADOW_TOKEN", "")
    if (not expected or not 32 <= len(expected) <= 256 or not expected.isascii()
            or any(c.isspace() for c in expected)):
        raise HTTPException(status_code=503, detail="shadow authentication not configured")
    if (not authorization or not authorization.isascii() or len(authorization) > 263
            or not authorization.startswith("Bearer ")):
        raise HTTPException(status_code=401, detail="unauthorized")
    supplied = authorization[7:]
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/internal/v1/shadow")
async def shadow_route(
    request: Request,
    authorization: str | None = Header(default=None),
    x_aj_service_auth: str | None = Header(default=None),
    x_aj_correlation_id: str | None = Header(default=None),
):
    cid = correlation_id(x_aj_correlation_id)
    counts = Counter(name.lower() for name, _ in request.scope['headers'])
    if (any(counts[name] > 1 for name in (b'authorization', b'x-aj-service-auth', b'x-aj-correlation-id', b'content-length'))
            or request.url.query or request.scope.get('raw_path',b'/internal/v1/shadow') != b'/internal/v1/shadow'):
        raise HTTPException(status_code=401, detail="unauthorized")
    if x_aj_service_auth is not None and (not x_aj_service_auth.isascii() or len(x_aj_service_auth) > service_auth.MAX_HEADER_CHARS):
        raise HTTPException(status_code=401, detail="unauthorized")
    # Stream-counted bound also covers absent/false Content-Length and chunked uploads.
    chunks = bytearray()
    try:
        async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="shadow metadata too large")
                chunks.extend(chunk)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="shadow metadata timeout") from None
    body = bytes(chunks)
    await run_in_threadpool(_authenticate, authorization=authorization, credential=x_aj_service_auth, body=body)
    # Authenticate before schema validation to avoid leaking validation details to unauthenticated clients.
    try:
        # Reject duplicate metadata fields before Pydantic's last-key-wins parser.
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate metadata")
                result[key] = value
            return result
        json.loads(body, object_pairs_hook=unique)
        req = ShadowRequest.model_validate_json(body)
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="invalid shadow metadata") from None
    # Do not log task, tokens, provider identity, secrets or user content.
    log.info("shadow_received correlation_id=%s mode=shadow_only", cid)
    return JSONResponse(
        content={
            "mode": "shadow_only",
            "task_family": req.task_family,
            "data_class": classify_data(RoutingContext(req.task_family, "personal", "internal")),
            "eligible_providers": [],
            "billable_execution": False,
        },
        headers={CORRELATION_HEADER: cid},
    )
