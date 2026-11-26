"""Provider translation for the Responses-compatible endpoint.

OpenAI: forwarded to its own Responses API (full history replayed, `store`
off, no provider chaining), so hosted tools such as web search behave exactly
as before.

Anthropic: translated to the Messages API.
  instructions + system/developer messages -> system
  user/assistant messages                  -> messages (images as image blocks)
  function_call                            -> assistant tool_use block
  function_call_output                     -> user tool_result block
  function tools                           -> tools (input_schema)
  web_search tool                          -> Anthropic's server web search tool
  tool_choice auto/none/required/function  -> auto/none/any/tool
  text.format json                         -> a JSON-only instruction
and the reply back: text -> message item, tool_use -> function_call item.

`can_serve` says up front whether a provider can carry a request (e.g. an
OpenAI-only hosted tool), so the router skips it instead of failing mid-call.
"""
from __future__ import annotations

import json
import re
import secrets

from .items import Call, Conversation, Image, Msg, Result, Text, to_responses_items

WEB_SEARCH_TYPES = ("web_search", "web_search_preview", "web_search_2025_08_26")
ANTHROPIC_WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|jpg|gif|webp));base64,(.+)$", re.S | re.I)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def tool_kinds(tools: list) -> set[str]:
    kinds = set()
    for t in tools or []:
        kind = str((t or {}).get("type") or "")
        kinds.add("function" if kind == "function" else ("web_search" if kind.startswith("web_search") else kind))
    return kinds


def can_serve(provider: str, tools: list, convo: Conversation) -> str | None:
    """None if the provider can take this request, else a short reason."""
    if provider == "anthropic":
        unsupported = tool_kinds(tools) - {"function", "web_search"}
        if unsupported:
            return "tool_unsupported:" + ",".join(sorted(unsupported))
        for item in convo.items:
            if isinstance(item, Msg):
                for p in item.parts:
                    if isinstance(p, Image) and not (p.url.startswith("https://") or _DATA_URL.match(p.url)):
                        return "image_unsupported"
    return None


def wants_json(text_cfg) -> bool:
    fmt = (text_cfg or {}).get("format") if isinstance(text_cfg, dict) else None
    return isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")


# ---------------------------------------------------------------- OpenAI ----
def openai_request(model: str, instructions: str | None, convo: Conversation, req: dict) -> dict:
    body = {"model": model, "input": to_responses_items(convo), "store": False}
    if instructions:
        body["instructions"] = instructions
    for key in ("tools", "tool_choice", "max_output_tokens", "temperature", "top_p", "text",
                "parallel_tool_calls", "reasoning", "truncation"):
        if req.get(key) is not None:
            body[key] = req[key]
    return body


def openai_output(payload: dict) -> tuple[list[dict], dict, str]:
    """-> (output items, usage {input_tokens, output_tokens}, status)."""
    items = [i for i in (payload.get("output") or []) if isinstance(i, dict) and i.get("type") != "reasoning"]
    usage = payload.get("usage") or {}
    return items, {"input_tokens": int(usage.get("input_tokens") or 0),
                   "output_tokens": int(usage.get("output_tokens") or 0)}, str(payload.get("status") or "completed")


# ------------------------------------------------------------- Anthropic ----
def _image_block(img: Image) -> dict:
    m = _DATA_URL.match(img.url)
    if m:
        media = m.group(1).lower().replace("jpg", "jpeg")
        data = re.sub(r"\s+", "", m.group(2))
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": img.url}}


def _anthropic_messages(convo: Conversation) -> tuple[list[str], list[dict]]:
    system, messages = [], []

    def push(role, block):
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})

    for item in convo.items:
        if isinstance(item, Msg):
            if item.role == "system":
                system.extend(p.text for p in item.parts if isinstance(p, Text) and p.text)
                continue
            for p in item.parts:
                if isinstance(p, Text):
                    if p.text:
                        push(item.role, {"type": "text", "text": p.text})
                elif item.role == "user":
                    push("user", _image_block(p))
        elif isinstance(item, Call):
            try:
                args = json.loads(item.arguments) if item.arguments else {}
            except ValueError:
                args = {"_raw": item.arguments}
            push("assistant", {"type": "tool_use", "id": item.call_id, "name": item.name,
                               "input": args if isinstance(args, dict) else {"value": args}})
        elif isinstance(item, Result):
            push("user", {"type": "tool_result", "tool_use_id": item.call_id, "content": item.output or ""})
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    return system, messages


def _anthropic_tools(tools: list) -> list[dict]:
    out = []
    for t in tools or []:
        kind = str(t.get("type") or "")
        if kind == "function":
            out.append({"name": t["name"], "description": t.get("description") or "",
                        "input_schema": t.get("parameters") or {"type": "object", "properties": {}}})
        elif kind.startswith("web_search"):
            out.append(dict(ANTHROPIC_WEB_SEARCH))
    return out


def _anthropic_tool_choice(choice):
    if choice in (None, "auto"):
        return None
    if choice == "none":
        return {"type": "none"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        return {"type": "tool", "name": choice["name"]}
    return None


def anthropic_request(model: str, instructions: str | None, convo: Conversation, req: dict) -> dict:
    system, messages = _anthropic_messages(convo)
    if instructions:
        system.insert(0, instructions)
    if wants_json(req.get("text")):
        system.append("Respond with a single valid JSON object and nothing else.")
    body = {"model": model, "messages": messages,
            "max_tokens": int(req.get("max_output_tokens") or 4096)}
    if system:
        body["system"] = "\n\n".join(system)
    tools = _anthropic_tools(req.get("tools"))
    if tools:
        body["tools"] = tools
        choice = _anthropic_tool_choice(req.get("tool_choice"))
        if choice:
            body["tool_choice"] = choice
    if req.get("temperature") is not None:
        body["temperature"] = min(float(req["temperature"]), 1.0)
    return body


def anthropic_output(payload: dict) -> tuple[list[dict], dict, str]:
    texts, calls = [], []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append({"type": "function_call", "id": new_id("fc"), "call_id": block.get("id") or new_id("call"),
                          "name": block.get("name") or "", "arguments": json.dumps(block.get("input") or {}),
                          "status": "completed"})
    items = []
    if texts:
        items.append({"type": "message", "id": new_id("msg"), "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": "".join(texts), "annotations": []}]})
    items.extend(calls)
    usage = payload.get("usage") or {}
    stop = payload.get("stop_reason")
    status = "incomplete" if stop == "max_tokens" else "completed"
    return items, {"input_tokens": int(usage.get("input_tokens") or 0),
                   "output_tokens": int(usage.get("output_tokens") or 0)}, status


def items_to_conversation(items: list[dict]) -> list:
    """Output items -> neutral items, to remember a reply for the next turn of a chain."""
    out = []
    for i in items:
        if i.get("type") == "message":
            text = "".join(c.get("text") or "" for c in i.get("content") or [] if isinstance(c, dict))
            out.append(Msg("assistant", (Text(text),) if text else ()))
        elif i.get("type") == "function_call":
            out.append(Call(str(i.get("call_id") or ""), str(i.get("name") or ""), str(i.get("arguments") or "{}")))
    return out
