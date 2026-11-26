"""Live smoke test: one tiny, non-personal call per configured provider.

    python -m aj_llm_layer.gateway.smoke            # each provider on its own
    python -m aj_llm_layer.gateway.smoke --fallback # full gateway order

Makes real, billable API calls (a few tokens each). Prints metadata only.
"""
from __future__ import annotations

import json
import sys

from . import CompletionRequest, Gateway, GatewayError, Message


def main(argv: list[str]) -> int:
    gateway = Gateway()
    print(json.dumps({"config": gateway.status()}, indent=2))
    base = dict(messages=(Message(role="user", content="Reply with the single word: ready"),),
                data_class="non_personal", tier="fast", max_tokens=16, purpose="smoke_test")
    runs = [None] if "--fallback" in argv else [(p.name,) for p in gateway.config.providers]
    failed = False
    for providers in runs:
        try:
            r = gateway.complete(CompletionRequest(**base, providers=providers), app="smoke")
            print(json.dumps({"ok": True, "provider": r.provider, "model": r.model,
                              "latency_ms": r.latency_ms, "fallback_used": r.fallback_used,
                              "reply": r.text.strip()[:40]}))
        except GatewayError as exc:
            failed = True
            print(json.dumps({"ok": False, "providers": providers, "error": exc.kind.value,
                              "attempts": [a.model_dump() for a in exc.attempts]}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
