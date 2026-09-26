#!/usr/bin/env python3
"""Measure C1 decode after exact-length, uncached synthetic prompts.

Raw /v1/completions with benchmark-prefill.py's exact-length filler (unique
first cache block per request) followed by forced-length output.  This is a
serving capacity/speed measurement, not a long-context quality test.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import glm53_bench as common

prefill = common.load_sibling("benchmark-prefill.py")


def measure(base_url: str, model: str, prompt, output_tokens: int, *, temperature: float = 0,
            seed: int | None = None, timeout: float = 7200) -> dict:
    result = common.stream(
        base_url, "/v1/completions",
        common.completion_payload(model, prompt, output_tokens, temperature=temperature,
                                  seed=seed, force_length=True),
        timeout)
    timing = common.token_timing(result)
    if timing["completion_tokens_streamed"] != output_tokens or not timing["token_count_matches_usage"]:
        raise RuntimeError(f"incomplete token evidence: {timing}, usage={result['usage']}")
    return {
        "usage": result["usage"],
        "finish_reason": result["finish_reason"],
        "content": result["content"],
        "chunks": [{"seconds": c["seconds"], "token_ids": c["token_ids"]}
                   for c in result["chunks"] if c["token_ids"]],
        "ttft_seconds": timing["ttft_seconds"],
        "decode_seconds": timing["decode_seconds"],
        "decode_tokens": timing["decode_tokens"],
        "decode_tps": timing["n_minus_one_tps"],
        "post_burst_tps": timing["post_burst_tps"],
        "request_seconds": result["request_seconds"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="server root or .../v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--depths", type=int, nargs="+",
                        default=[2048, 8192, 32768, 131072, 262144, 524288])
    parser.add_argument("--output-tokens", type=int, default=256)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--collect-spec-metrics", action="store_true",
                        help="per-request spec-decode deltas (C1; requires an exclusive server)")
    parser.add_argument("--metrics-wait", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0 or args.output_tokens < 2:
        parser.error("require runs >= 1, warmups >= 0 and output tokens >= 2")
    args.base_url = common.server_root(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps({
            "record": "meta", "schema": "glm53-context-v1",
            "args": vars(args) | {"output": str(args.output)},
            "workload": "C1 forced-length raw completion; synthetic filler with a unique "
                        "first block; decode_tps = (N-1)/first-to-last token window",
        }) + "\n")
        for depth in args.depths:
            for run in range(-args.warmups, args.runs):
                nonce = uuid.uuid4().hex
                prompt = prefill.exact_prompt(args.base_url, args.model, depth, nonce)
                before = (common.spec_snapshot(args.base_url, args.metrics_wait)
                          if args.collect_spec_metrics and run >= 0 else None)
                result = measure(args.base_url, args.model, prompt, args.output_tokens,
                                 timeout=args.timeout)
                after = (common.spec_snapshot(args.base_url, args.metrics_wait)
                         if before is not None else None)
                if result["usage"]["prompt_tokens"] != depth:
                    raise RuntimeError(
                        f"server counted {result['usage']['prompt_tokens']} prompt tokens, expected {depth}")
                row = {"record": "measurement", "depth": depth, "run": run, "timed": run >= 0,
                       "nonce": nonce, "spec_decode": common.spec_delta(before, after), **result}
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                print(json.dumps({k: row[k] for k in ("depth", "run", "ttft_seconds", "decode_tps")}),
                      flush=True)


if __name__ == "__main__":
    main()
