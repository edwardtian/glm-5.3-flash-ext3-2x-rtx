#!/usr/bin/env python3
"""Exact-key retrieval at several positions in long synthetic archives, thinking off.

One random key per prompt, placed at a character fraction of exact-length
filler from benchmark-prefill.py (unique first block).  The chat is rendered
with thinking closed (glm53_bench.render_chat) and answered greedily.  For a
six-record single-prompt check see test-multi-needle-vllm.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

import glm53_bench as common

prefill = common.load_sibling("benchmark-prefill.py")


def normalized(text: str) -> str:
    return text.strip().strip("`'\"").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="server root or .../v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--filler-tokens", type=int, nargs="+", default=[8192, 240000, 1000000])
    parser.add_argument("--positions", type=float, nargs="+", default=[0.05, 0.5, 0.95])
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--seed", type=int, default=787)
    parser.add_argument("--timeout", type=float, default=14400)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(not 0 <= position <= 1 for position in args.positions):
        parser.error("positions must be between zero and one")
    args.base_url = common.server_root(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    passed = True
    with args.output.open("x") as output:
        output.write(json.dumps({
            "record": "meta", "schema": "glm53-context-retrieval-v1",
            "args": vars(args) | {"output": str(args.output)},
            "method": "one synthetic key per prompt; positions are character fractions of "
                      "exact-length filler; thinking closed at render time; pass = response "
                      "equals the key after trimming whitespace, quotes and backticks; actual "
                      "prompt length comes from server usage",
        }) + "\n")
        for depth in args.filler_tokens:
            for position in args.positions:
                nonce = uuid.uuid4().hex
                key = hashlib.sha256(f"{args.seed}:{depth}:{position}".encode()).hexdigest()[:20]
                filler = prefill.exact_prompt(args.base_url, args.model, depth, nonce)
                offset = round(len(filler) * position)
                prompt = (
                    "Read this archive and find its ARCHIVE_VERIFICATION_KEY.\n"
                    + filler[:offset] + f"\nARCHIVE_VERIFICATION_KEY = {key}\n" + filler[offset:]
                    + "\nEnd of archive. Return only the exact ARCHIVE_VERIFICATION_KEY, without explanation."
                )
                rendered = common.render_chat(
                    args.base_url, args.model, [{"role": "user", "content": prompt}],
                    thinking=False, reasoning_effort=args.reasoning_effort, timeout=args.timeout)
                result = common.stream(
                    args.base_url, "/v1/completions",
                    common.completion_payload(args.model, rendered["token_ids"], args.max_tokens,
                                              temperature=0, seed=args.seed),
                    args.timeout)
                timing = common.token_timing(result)
                correct = normalized(result["content"]) == key
                passed &= correct
                row = {"record": "measurement", "filler_tokens": depth,
                       "position_fraction": position, "character_offset": offset,
                       "nonce": nonce, "expected_key": key,
                       "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                       "prompt_tokens": len(rendered["token_ids"]),
                       "thinking_control": rendered["method"],
                       "passed": correct, "key_in_response": key in result["content"],
                       "content": result["content"], "usage": result["usage"],
                       "finish_reason": result["finish_reason"],
                       "ttft_seconds": timing["ttft_seconds"],
                       "request_seconds": result["request_seconds"]}
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(json.dumps({"filler_tokens": depth, "position": position,
                                  "prompt_tokens": row["prompt_tokens"], "passed": correct,
                                  "response": result["content"][:200]}), flush=True)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
