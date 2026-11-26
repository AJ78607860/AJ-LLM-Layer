#!/usr/bin/env python3
"""Drives the Layer with AJ's pinned OpenAI SDK (3.20.0), the way aj.py's turn loop does."""
import json, os, sys
import openai
from openai import OpenAI
base, key = sys.argv[1], sys.argv[2]
client = OpenAI(base_url=base, api_key=key, max_retries=0, default_headers={"X-AJ-Data-Class": "sensitive"})
TOOLS = [{"type": "function", "name": "get_weather", "description": "Weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or sys.exit(1))
print("openai SDK", openai.__version__)
# 1. first call with tools (as aj.py: model, instructions, input, tools, max_output_tokens)
r = client.responses.create(model="gpt-5.4", instructions="You are AJ.", input=[{"role": "user", "content": "What's the weather in Leeds?"}],
                            tools=TOOLS, max_output_tokens=800)
calls = [i for i in r.output if i.type == "function_call"]
ok(len(calls) == 1 and calls[0].name == "get_weather" and json.loads(calls[0].arguments) == {"city": "Leeds"},
   f"tool call parsed by SDK: {calls[0].name}({calls[0].arguments}) call_id={calls[0].call_id}")
ok(r.id.startswith("resp_") and r.usage.input_tokens == 40, f"response id + usage parsed ({r.id[:12]}…, in={r.usage.input_tokens})")
# 2. follow-up: only the tool output + previous_response_id (aj.py line ~7217)
r2 = client.responses.create(model="gpt-5.4", instructions="You are AJ.", previous_response_id=r.id, tools=TOOLS,
                             input=[{"type": "function_call_output", "call_id": calls[0].call_id, "output": "sunny"}])
ok(r2.output_text == "It is sunny in Leeds." and not [i for i in r2.output if i.type == "function_call"],
   f"follow-up answered via chain: {r2.output_text!r}")
# 3. expired / unknown chain -> SDK error AJ's is_chain_error() treats as 'rebuild'
try:
    client.responses.create(model="gpt-5.4", previous_response_id="resp_doesnotexist", input="more")
    ok(False, "unknown chain should fail")
except openai.BadRequestError as e:
    ok(e.status_code == 400 and "previous_response" in str(e).lower(), f"unknown chain -> 400 BadRequestError mentioning previous_response")
# 4. both providers out of credit -> 429 insufficient_quota, classified like OpenAI's own
open(os.environ["STATE"] + "/anthropic_out", "w").close(); open(os.environ["STATE"] + "/openai_out", "w").close()
try:
    client.responses.create(model="gpt-5.4", input="hello")
    ok(False, "should be out of credit")
except openai.RateLimitError as e:
    body = e.body if isinstance(e.body, dict) else {}
    ok(e.status_code == 429 and (body.get("code") == "insufficient_quota" or body.get("error", {}).get("code") == "insufficient_quota"),
       "all providers out of credit -> RateLimitError code=insufficient_quota (AJ: CREDIT_EXHAUSTED)")
print("ALL CONTRACT CHECKS PASSED")
