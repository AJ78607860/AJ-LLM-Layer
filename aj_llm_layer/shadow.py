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

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import service_auth
from .policy import RoutingContext, classify_data

router = APIRouter()
log = logging.getLogger("aj.llm_layer.shadow")
CORRELATION_HEADER = "X-AJ-Correlation-ID"
_CORRELATION_RE = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")
_replay = service_auth.ReplayCache()


class ShadowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_family: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    estimated_input_tokens: int = Field(ge=0, le=200000)
    estimated_output_tokens: int = Field(ge=0, le=200000)


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
                body=body, keys=keys, env=env, scope="shadow:write", replay=_replay,
            )
        except service_auth.InvalidCredential as exc:
            # Safe fixed reason only; never log signed tokens or request bodies.
            log.warning("shadow_auth_rejected reason=%s", exc.reason)
            raise HTTPException(status_code=401, detail="unauthorized") from None
        return

    expected = os.getenv("AJ_LLM_LAYER_SHADOW_TOKEN", "")
    if not expected or len(expected) < 32 or any(c.isspace() for c in expected):
        raise HTTPException(status_code=503, detail="shadow authentication not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/internal/v1/shadow")
async def shadow_route(
    req: ShadowRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_aj_service_auth: str | None = Header(default=None),
    x_aj_correlation_id: str | None = Header(default=None),
):
    cid = correlation_id(x_aj_correlation_id)
    _authenticate(authorization=authorization, credential=x_aj_service_auth, body=await request.body())
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
