"""Neutral conversation items for the OpenAI-Responses-compatible endpoint.

AJ (and any app built on the OpenAI SDK) sends `input` as a string or a list of
Responses "items". We normalise them into three neutral kinds that every
provider adapter understands:

* ``Msg(role, parts)``   role: user | assistant | system; parts are text or images
* ``Call(call_id, name, arguments)``  the model asked to run a tool
* ``Result(call_id, output)``         the app's answer to that tool call

Anything we cannot represent is reported, so the caller can either skip a
provider that can't take it or refuse the request; nothing is silently dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


class UnsupportedInput(ValueError):
    """The request contains something this endpoint cannot carry."""


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Image:
    url: str                 # https URL or data: URL
    detail: str | None = None


@dataclass(frozen=True)
class Msg:
    role: str                # user | assistant | system
    parts: tuple = ()


@dataclass(frozen=True)
class Call:
    call_id: str
    name: str
    arguments: str           # JSON text, exactly as produced by the model


@dataclass(frozen=True)
class Result:
    call_id: str
    output: str


@dataclass
class Conversation:
    items: list = field(default_factory=list)

    def text_for_routing(self, limit: int = 300_000) -> str:
        """All message text, newest last, for task reading and token estimates (never logged)."""
        chunks = []
        for item in self.items:
            if isinstance(item, Msg):
                chunks.extend(p.text for p in item.parts if isinstance(p, Text))
            elif isinstance(item, Result):
                chunks.append(item.output)
        return "\n".join(chunks)[-limit:]

    def latest_user_text(self) -> str:
        for item in reversed(self.items):
            if isinstance(item, Msg) and item.role == "user":
                text = " ".join(p.text for p in item.parts if isinstance(p, Text)).strip()
                if text:
                    return text
        return ""

    def has_images(self) -> bool:
        return any(isinstance(p, Image) for i in self.items if isinstance(i, Msg) for p in i.parts)


_ROLE = {"user": "user", "assistant": "assistant", "system": "system", "developer": "system"}


def _parts(content) -> tuple:
    if content is None:
        return ()
    if isinstance(content, str):
        return (Text(content),) if content else ()
    if not isinstance(content, list):
        raise UnsupportedInput("message content must be a string or a list")
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(Text(part))
            continue
        if not isinstance(part, dict):
            raise UnsupportedInput("content part must be an object")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            parts.append(Text(str(part.get("text") or "")))
        elif kind == "refusal":
            parts.append(Text(str(part.get("refusal") or "")))
        elif kind == "input_image":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url or not isinstance(url, str):
                raise UnsupportedInput("input_image needs image_url (file_id images are not supported)")
            parts.append(Image(url=url, detail=part.get("detail")))
        else:
            raise UnsupportedInput(f"content part type {kind!r} is not supported")
    return tuple(parts)


def parse_input(raw) -> Conversation:
    """Responses `input` (string or item list) -> Conversation."""
    convo = Conversation()
    if raw is None:
        return convo
    if isinstance(raw, str):
        convo.items.append(Msg("user", (Text(raw),)))
        return convo
    if not isinstance(raw, list):
        raise UnsupportedInput("input must be a string or a list of items")
    for item in raw:
        if not isinstance(item, dict):
            raise UnsupportedInput("each input item must be an object")
        kind = item.get("type")
        if kind in (None, "message") and "role" in item:
            role = _ROLE.get(item.get("role"))
            if role is None:
                raise UnsupportedInput(f"role {item.get('role')!r} is not supported")
            convo.items.append(Msg(role, _parts(item.get("content"))))
        elif kind == "function_call":
            args = item.get("arguments")
            convo.items.append(Call(str(item.get("call_id") or item.get("id") or ""), str(item.get("name") or ""),
                                    args if isinstance(args, str) else json.dumps(args or {})))
        elif kind == "function_call_output":
            out = item.get("output")
            convo.items.append(Result(str(item.get("call_id") or ""),
                                      out if isinstance(out, str) else json.dumps(out)))
        elif kind in ("reasoning", "web_search_call"):
            continue  # provider-internal; never needed to continue a conversation
        else:
            raise UnsupportedInput(f"input item type {kind!r} is not supported")
    return convo


def to_responses_items(convo: Conversation) -> list[dict]:
    """Conversation -> Responses input items (for replaying history to OpenAI, without provider ids)."""
    out = []
    for item in convo.items:
        if isinstance(item, Msg):
            role = "developer" if item.role == "system" else item.role
            text_type = "output_text" if item.role == "assistant" else "input_text"
            content = []
            for p in item.parts:
                if isinstance(p, Text):
                    content.append({"type": text_type, "text": p.text})
                elif item.role != "assistant":
                    content.append({"type": "input_image", "image_url": p.url,
                                    **({"detail": p.detail} if p.detail else {})})
            out.append({"role": role, "content": content})
        elif isinstance(item, Call):
            out.append({"type": "function_call", "call_id": item.call_id, "name": item.name,
                        "arguments": item.arguments})
        elif isinstance(item, Result):
            out.append({"type": "function_call_output", "call_id": item.call_id, "output": item.output})
    return out
