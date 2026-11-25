"""Provider-neutral request/response contracts for the execution gateway.

Every app (AJ Assistant, Calibre, ...) speaks only these types. Provider
adapters translate them to and from each vendor's wire format.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Role = Literal["user", "assistant"]
Tier = Literal["fast", "standard", "smart"]
DataClass = Literal["personal", "non_personal"]

MAX_MESSAGES = 200
MAX_CHARS = 400_000  # total prompt characters accepted per request


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(_Strict):
    role: Role
    content: str = Field(min_length=1, max_length=MAX_CHARS)


class CompletionRequest(_Strict):
    """One model call. `data_class` decides which providers may see the content."""

    messages: tuple[Message, ...] = Field(min_length=1, max_length=MAX_MESSAGES)
    system: str | None = Field(default=None, max_length=MAX_CHARS)
    purpose: str = Field(default="general", pattern=r"^[a-z0-9_]{1,64}$")
    tier: Tier = "standard"
    data_class: DataClass = "personal"  # fail safe: assume personal unless the app says otherwise
    max_tokens: int = Field(default=1024, ge=1, le=64_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    json_mode: bool = False
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    providers: tuple[str, ...] | None = Field(default=None, max_length=8)
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._:/-]{1,100}$")

    @model_validator(mode="after")
    def _pinned_model(self):
        if self.model is not None and (not self.providers or len(self.providers) != 1):
            raise ValueError("an explicit model needs exactly one provider in `providers`")
        return self

    @field_validator("messages")
    @classmethod
    def _shape(cls, messages):
        if messages[-1].role != "user":
            raise ValueError("last message must be from the user")
        total = sum(len(m.content) for m in messages)
        if total > MAX_CHARS:
            raise ValueError("prompt too large")
        return messages


class Usage(_Strict):
    input_tokens: int = 0
    output_tokens: int = 0


class Attempt(_Strict):
    """Content-free record of one provider attempt (safe to log and return)."""

    provider: str
    model: str
    outcome: str  # "ok" or an ErrorKind value, or "skipped:<reason>"
    latency_ms: int = 0


class CompletionResult(_Strict):
    text: str
    provider: str
    model: str
    finish_reason: str
    usage: Usage
    cost_usd: float | None
    latency_ms: int
    fallback_used: bool
    attempts: tuple[Attempt, ...]
