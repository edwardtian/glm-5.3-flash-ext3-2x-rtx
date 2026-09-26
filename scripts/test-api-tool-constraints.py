#!/usr/bin/env python3
"""Verify required/named/auto/none tool choices, thinking off/on, streaming/non-streaming.

Thinking is toggled with chat_template_kwargs.enable_thinking, as API clients
do.  The served GLM templates open <think> regardless; the glm45 reasoning
parser reads the flag.  Each row records how much reasoning text the API
returned so a thinking-off leak is visible without failing the tool contract.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

import glm53_bench as common


def request(base: str, payload: dict, timeout: float):
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if not payload["stream"]:
            body = json.load(response)
            message = body["choices"][0]["message"]
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            return (message.get("tool_calls") or [], body, len(reasoning),
                    body["choices"][0].get("finish_reason"))
        events, calls, reasoning, finish = [], {}, 0, None
        for line in response:
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                break
            event = json.loads(raw)
            events.append(event)
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                reasoning += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
                finish = choice.get("finish_reason") or finish
                for call in delta.get("tool_calls") or []:
                    current = calls.setdefault(call["index"], {"function": {"name": "", "arguments": ""}})
                    for field in ("name", "arguments"):
                        current["function"][field] += (call.get("function") or {}).get(field) or ""
        return [calls[i] for i in sorted(calls)], events, reasoning, finish


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="server root or .../v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="covers template-default (max) reasoning before the tool call")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.base_url = common.server_root(args.base_url)
    tool = {"type": "function", "function": {
        "name": "calculator", "description": "Evaluate arithmetic.",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                       "required": ["expression"], "additionalProperties": False}}}
    cases = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps({"record": "meta", "schema": "glm53-api-tool-constraints-v1",
                                 "args": vars(args) | {"output": str(args.output)}}) + "\n")
        for thinking in (False, True):
            for streaming in (False, True):
                for mode in ("required", "named", "auto", "none"):
                    payload = {
                        "model": args.model,
                        "messages": [
                            {"role": "system", "content": "Use the calculator if the user asks for it. Otherwise answer directly."},
                            {"role": "user", "content": "What is 7 times 8?" + (" Use the calculator." if mode == "auto" else "")}],
                        "tools": [tool],
                        "tool_choice": mode if mode != "named" else {"type": "function", "function": {"name": "calculator"}},
                        "temperature": 0, "max_tokens": args.max_tokens,
                        "chat_template_kwargs": {"enable_thinking": thinking},
                        "stream": streaming,
                    }
                    if streaming:
                        payload["stream_options"] = {"include_usage": True}
                    error = None
                    try:
                        calls, raw, reasoning_chars, finish = request(args.base_url, payload, args.timeout)
                    except urllib.error.HTTPError as exc:
                        calls, raw, reasoning_chars, finish = [], None, 0, None
                        error = f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
                    correct = error is None and (not calls if mode == "none" else len(calls) == 1)
                    for call in calls:
                        try:
                            params = json.loads(call["function"]["arguments"])
                            correct &= call["function"]["name"] == "calculator"
                            correct &= set(params) == {"expression"}
                            correct &= params["expression"].replace(" ", "") in ("7*8", "8*7")
                        except (ValueError, KeyError, TypeError, AttributeError):
                            correct = False
                    row = {"record": "measurement", "thinking": thinking, "streaming": streaming,
                           "mode": mode, "passed": correct, "finish_reason": finish,
                           "reasoning_chars": reasoning_chars, "error": error,
                           "request": payload, "calls": calls, "raw_response": raw}
                    cases.append(row)
                    output.write(json.dumps(row) + "\n")
                    output.flush()
                    print(json.dumps({k: row[k] for k in ("thinking", "streaming", "mode", "passed",
                                                          "finish_reason", "reasoning_chars")}), flush=True)
    if not all(row["passed"] for row in cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
