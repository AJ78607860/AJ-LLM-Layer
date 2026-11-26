#!/bin/sh
# Contract test: AJ's pinned OpenAI SDK against the running Layer (/v1/responses), with local stand-ins for
# api.openai.com and api.anthropic.com. No real keys, no network beyond 127.0.0.1, nothing spent.
#   python3 -m venv /tmp/sdk && /tmp/sdk/bin/pip install openai==3.20.0 httpx2==2.13.1
#   SDK_PY=/tmp/sdk/bin/python LAYER_PY=python3 sh scripts/contract/run.sh            # random route
#   ONLY=openai SDK_PY=... LAYER_PY=... sh scripts/contract/run.sh                       # force the OpenAI route
set -eu
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
STATE=$(mktemp -d); KEY=$(python3 -c 'print("a"*40)')
DISABLED=""; [ "${ONLY:-}" = openai ] && DISABLED=claude-haiku-5-5,claude-sonnet-5-5,claude-opus-5-5
[ "${ONLY:-}" = anthropic ] && DISABLED=gpt-6.1-terra,gpt-6.2-sol,gpt-6.2-astra
python3 "$HERE/upstream_standin.py" 8911 "$STATE" & UP=$!
env -i PATH=/usr/bin:/bin HOME="$STATE" PYTHONPATH="$ROOT" AJ_LLM_GATEWAY_ENABLED=true \
  AJ_LLM_GATEWAY_KEYS="aj_assistant:$KEY" OPENAI_API_KEY=sk-o ANTHROPIC_API_KEY=sk-a \
  AJ_LLM_PERSONAL_DATA_PROVIDERS=openai,anthropic AJ_LLM_EXPLORE_RATE=0 AJ_LLM_DISABLED_MODELS="$DISABLED" \
  AJ_LLM_OPENAI_BASE_URL=http://127.0.0.1:8911/openai AJ_LLM_ANTHROPIC_BASE_URL=http://127.0.0.1:8911/anthropic \
  "${LAYER_PY:-python3}" -m uvicorn aj_llm_layer.api:app --port 8912 --log-level warning > "$STATE/server.log" 2>&1 & SRV=$!
trap 'kill $SRV $UP 2>/dev/null' EXIT
sleep 3
STATE="$STATE" "${SDK_PY:?set SDK_PY to a python with openai==3.20.0}" "$HERE/aj_sdk_driver.py" http://127.0.0.1:8912/v1 "$KEY"
if grep -qE 'sk-o|sk-a|aaaaaaaa|Leeds|sunny' "$STATE/server.log"; then echo "FAIL secrets or prompt text in Layer log"; exit 1; fi
echo "PASS Layer log holds no keys or prompt text"
