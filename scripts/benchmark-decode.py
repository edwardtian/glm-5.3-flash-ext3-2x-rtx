#!/usr/bin/env python3
"""Measure aggregate pure-decode throughput at selected concurrencies."""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import glm53_bench as common

PROSE_PROMPT = (
    "Write a long, coherent continuation about systems engineering, "
    "without headings or a conclusion."
)


def request_once(
    base_url: str,
    model: str,
    concurrency: int,
    output_tokens: int,
    seed: int,
    prompt_token_ids: list[int] | None = None,
) -> dict:
    parsed = urlparse(base_url)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=3600)
    path = parsed.path.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": PROSE_PROMPT,
            }
        ],
        "n": concurrency,
        "max_tokens": output_tokens,
        "min_tokens": output_tokens,
        "ignore_eos": True,
        "temperature": 0.7,
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
        "return_token_ids": True,
        "cache_prompt": False,
    }
    if prompt_token_ids is not None:
        # Thinking-off rendered prompt (see glm53_bench.render_chat).
        path = common.server_root(parsed.path) + "/v1/completions"
        del payload["messages"]
        payload["prompt"] = prompt_token_ids
        payload["add_special_tokens"] = False
    started = time.perf_counter()
    connection.request(
        "POST",
        path,
        body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    if response.status != 200:
        error = response.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {response.status}: {error}")

    token_times: list[list[float]] = [[] for _ in range(concurrency)]
    completion_tokens = None
    while True:
        raw_line = response.readline()
        if not raw_line:
            break
        observed = time.perf_counter()
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        if isinstance(event.get("usage"), dict):
            completion_tokens = event["usage"].get("completion_tokens")
        for choice in event.get("choices", []):
            index = choice.get("index")
            if not isinstance(index, int) or not 0 <= index < concurrency:
                continue
            token_ids = choice.get("token_ids")
            if isinstance(token_ids, list):
                token_times[index].extend([observed] * len(token_ids))
    connection.close()

    expected = concurrency * output_tokens
    observed_tokens = sum(len(times) for times in token_times)
    if observed_tokens != expected:
        raise RuntimeError(
            f"stream exposed {observed_tokens} token IDs, expected {expected}; "
            f"usage reported {completion_tokens}"
        )
    first_token = min(times[0] for times in token_times if times)
    last_token = max(times[-1] for times in token_times if times)
    duration = last_token - first_token
    decode_tokens = sum(len(times) - 1 for times in token_times)
    return {
        "concurrency": concurrency,
        "started_perf_seconds": started,
        "first_token_perf_seconds": first_token,
        "last_token_perf_seconds": last_token,
        "output_tokens_per_sequence": output_tokens,
        "completion_tokens": completion_tokens,
        "decode_tokens": decode_tokens,
        "decode_seconds": duration,
        "decode_tokens_per_second": decode_tokens / duration,
        "ttft_ms": (first_token - started) * 1000,
    }


def independent_clients(
    base_url: str,
    model: str,
    concurrency: int,
    output_tokens: int,
    seed: int,
    thinking: str = "template",
    reasoning_effort: str | None = "low",
) -> dict:
    """C separate HTTP requests released together, each with its own seed."""
    nonce = uuid.uuid4().hex
    prompts: list[list[int] | None] = []
    rendered_info = None
    for index in range(concurrency):
        if thinking == "off":
            rendered = common.render_chat(
                base_url,
                model,
                [{"role": "user", "content": f"Ignore this request identifier: {nonce}:{index}.\n" + PROSE_PROMPT}],
                thinking=False,
                reasoning_effort=reasoning_effort,
            )
            prompts.append(rendered["token_ids"])
            rendered_info = rendered["method"]
        else:
            prompts.append(None)
    barrier = threading.Barrier(concurrency)

    def worker(index: int) -> dict:
        barrier.wait()
        return request_once(
            base_url, model, 1, output_tokens, seed + index, prompt_token_ids=prompts[index]
        )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(worker, range(concurrency)))
    first = min(r["first_token_perf_seconds"] for r in results)
    last = max(r["last_token_perf_seconds"] for r in results)
    tokens = sum(r["decode_tokens"] for r in results)
    events = []
    for result in results:
        events.extend(
            ((result["first_token_perf_seconds"], 1), (result["last_token_perf_seconds"], -1))
        )
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return {
        "concurrency": concurrency,
        "request_mode": "clients",
        "request_nonce": nonce,
        "thinking_control": rendered_info or "template default (chat completions)",
        "peak_overlapping_stream_intervals": peak,
        "decode_tokens": tokens,
        "decode_seconds": last - first,
        "decode_tokens_per_second": tokens / (last - first),
        "per_request_decode_tokens_per_second": [
            r["decode_tokens_per_second"] for r in results
        ],
        "request_results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile", required=True, choices=["nvfp4", "fp8"])
    parser.add_argument("--mtp-tokens", type=int, required=True)
    parser.add_argument(
        "--mtp-policy",
        choices=["off", "static", "adaptive"],
        default="static",
    )
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 16])
    parser.add_argument(
        "--request-mode",
        choices=["continuations", "clients"],
        default="continuations",
        help="continuations: one request with n=C; clients: C independent requests",
    )
    parser.add_argument(
        "--thinking",
        choices=["template", "off"],
        default="template",
        help="clients mode only: 'off' renders with thinking closed and uses /v1/completions",
    )
    parser.add_argument("--reasoning-effort", default="low",
                        help="effort rendered with --thinking off")
    parser.add_argument(
        "--collect-spec-metrics",
        action="store_true",
        help="record spec-decode counter deltas around each measured run",
    )
    parser.add_argument("--metrics-wait", type=float, default=2.0)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument(
        "--warmup-tokens",
        type=int,
        default=0,
        help="warmup length; 0 uses --output-tokens so the full decode path is warm",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=2,
        help="full warmup requests per concurrency (two covers lazy JIT shapes)",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.thinking == "off" and args.request_mode != "clients":
        parser.error("--thinking off requires --request-mode clients")

    if args.request_mode == "clients":
        def runner(base_url, model, concurrency, tokens, seed):
            return independent_clients(
                base_url, model, concurrency, tokens, seed,
                thinking=args.thinking, reasoning_effort=args.reasoning_effort,
            )
    else:
        runner = request_once

    points = []
    warmup_tokens = args.warmup_tokens or args.output_tokens
    for concurrency in args.concurrency:
        for _ in range(args.warmup_runs):
            runner(
                args.base_url, args.model, concurrency, warmup_tokens, args.seed
            )
        runs = []
        for run in range(args.runs):
            before = (
                common.spec_snapshot(args.base_url, args.metrics_wait)
                if args.collect_spec_metrics else None
            )
            result = runner(
                args.base_url,
                args.model,
                concurrency,
                args.output_tokens,
                args.seed,
            )
            if args.collect_spec_metrics:
                after = common.spec_snapshot(args.base_url, args.metrics_wait)
                result["spec_decode"] = common.spec_delta(before, after)
            runs.append(result)
            print(
                f"{args.profile} {args.mtp_policy} MTP{args.mtp_tokens} "
                f"C{concurrency} "
                f"run {run + 1}/{args.runs}: "
                f"{result['decode_tokens_per_second']:.2f} tok/s",
                flush=True,
            )
        rates = [run["decode_tokens_per_second"] for run in runs]
        points.append(
            {
                "concurrency": concurrency,
                "aggregate_decode_tokens_per_second": {
                    "median": statistics.median(rates),
                    "min": min(rates),
                    "max": max(rates),
                },
                "runs": runs,
            }
        )

    if args.request_mode == "clients":
        schema = "glm53-decode-concurrency.v3"
        method = (
            "C separate HTTP requests with unique prompt identifiers released by a "
            "barrier; aggregate decode uses sum(N-1) over the global first-to-last "
            "SSE token window; sampling seeds are fixed per client (seed + index); "
            + ("thinking closed at render time" if args.thinking == "off"
               else "chat template default thinking")
        )
    else:
        schema = "glm53-decode-concurrency.v2"
        method = (
            "one depth-0 prompt with n parallel continuations; aggregate decode "
            "timing spans first to last streamed token and excludes TTFT; the "
            "same sampling seed is repeated so MTP acceptance is comparable"
        )
    report = {
        "schema": schema,
        "request_mode": args.request_mode,
        "thinking": args.thinking,
        "method": method,
        "model": args.model,
        "kv_cache_profile": args.profile,
        "mtp_tokens": args.mtp_tokens,
        "mtp_policy": args.mtp_policy,
        "output_tokens_per_sequence": args.output_tokens,
        "warmup_runs_per_point": args.warmup_runs,
        "runs_per_point": args.runs,
        "points": points,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
