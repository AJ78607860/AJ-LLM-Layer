#!/usr/bin/env python3
"""Stand-in for api.openai.com (/openai/responses) and api.anthropic.com (/anthropic/messages)."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
STATE = sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        with open(STATE + "/calls.log", "a") as f: f.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if self.path == "/anthropic/messages":
            if os.path.exists(STATE + "/anthropic_out"):
                return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}})
            last = body["messages"][-1]["content"]
            if body.get("tools") and not any(b.get("type") == "tool_result" for b in last):
                return self._send(200, {"content": [{"type": "tool_use", "id": "toolu_A1", "name": "get_weather", "input": {"city": "Leeds"}}],
                                        "stop_reason": "tool_use", "usage": {"input_tokens": 40, "output_tokens": 9}})
            result = [b for b in last if b.get("type") == "tool_result"]
            text = f"It is {result[0]['content']} in Leeds." if result else "Hello from Claude"
            return self._send(200, {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": {"input_tokens": 60, "output_tokens": 8}})
        if self.path == "/openai/responses":
            if os.path.exists(STATE + "/openai_out"):
                return self._send(429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}})
            items = body.get("input") or []
            outs = [i for i in items if i.get("type") == "function_call_output"]
            if body.get("tools") and not outs:
                return self._send(200, {"id": "resp_up", "status": "completed", "output": [{"type": "reasoning", "id": "rs1", "summary": []}, {"type": "function_call", "id": "fc1", "call_id": "call_O1", "name": "get_weather", "arguments": "{\"city\":\"Leeds\"}", "status": "completed"}], "usage": {"input_tokens": 40, "output_tokens": 9}})
            text = f"It is {outs[-1]['output']} in Leeds." if outs else "Hello from OpenAI"
            return self._send(200, {"id": "resp_up", "status": "completed", "output": [{"type": "message", "id": "m1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}], "usage": {"input_tokens": 60, "output_tokens": 8}})
        self._send(404, {"error": {"type": "not_found_error"}})
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
