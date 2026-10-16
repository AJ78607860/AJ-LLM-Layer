"""Read-only internal routing diagnostics; never accepts prompt content."""
from __future__ import annotations

import os
import secrets
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .policy import RoutingContext, classify_data

router = APIRouter()


class ShadowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_family: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    # No prompt, PII, model text, or user ID accepted.
    estimated_input_tokens: int = Field(ge=0, le=200000)
    estimated_output_tokens: int = Field(ge=0, le=200000)


@router.post("/internal/v1/shadow")
def shadow_route(req: ShadowRequest, authorization: str | None = Header(default=None)):
    expected = os.environ.get("AJ_LLM_LAYER_SHADOW_TOKEN", "")
    if not expected or not expected.isascii() or len(expected) < 32 or any(c.isspace() for c in expected):
        raise HTTPException(status_code=503, detail="shadow service not configured")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not supplied.isascii() or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    # Deliberately no provider egress, user-level decisions, or price claims.
    return {
        "mode": "shadow_only",
        "task_family": req.task_family,
        "data_class": classify_data(RoutingContext(req.task_family, "personal", "internal")),
        "eligible_providers": [],
        "billable_execution": False,
    }
