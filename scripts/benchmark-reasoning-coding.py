#!/usr/bin/env python3
"""Reasoning-on coding decode at C1/C2/C4: the release decision metric.

Each batch sends C chat requests simultaneously (independent HTTP clients,
prompts rotated across four realistic coding tasks) with thinking enabled at
the template default effort, temperature 1.0, top_p 0.95 and fixed per-request
seeds.  Per request: TTFT, reasoning/content/total tokens, finish reason and
first-to-last-token decode rate over ALL generated tokens (reasoning and
content) using the (N-1)/window convention.  Per concurrency: median/min/max
per-request rate and median/min/max batch aggregate rate
(sum of N-1 over the batch's global first-to-last token window).

Headline "reasoning-coding score" = mean of the C1, C2 and C4 per-request
medians.  Tuning decisions are ranked by this number.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import glm53_bench as common

SCORE_CONCURRENCIES = (1, 2, 4)


def prompts() -> list[dict]:
    dflash = common.load_sibling("benchmark-dflash2-vllm.py")
    return [
        {
            "id": "lru-ttl-cache",
            "prompt": (
                "Implement a thread-safe LRU cache with per-entry TTL in Python 3.12. "
                "Requirements: class TTLCache[K, V] with get(key) -> V | None, "
                "set(key, value, ttl_seconds: float | None = None), delete(key) -> bool, "
                "__len__, and a configurable max_size; expired entries must never be "
                "returned and must be evicted lazily on access and eagerly when the cache "
                "is full; LRU order must be O(1) per operation; the clock must be "
                "injectable for testing. Then write a pytest test module that covers "
                "eviction order, TTL expiry with a fake clock, overwrite semantics, "
                "delete, max_size=1, and concurrent access from several threads. "
                "Return the implementation and the tests as two Python code blocks."
            ),
        },
        {
            "id": "async-task-runner-fix",
            # Identical to the recipe's code-agent-depth / DFlash2 benchmark task.
            "prompt": dflash.CODE_AGENT_PROMPT,
        },
        {
            "id": "sql-migration-rollback",
            "prompt": (
                "We run PostgreSQL 16. The table orders(id bigserial primary key, "
                "customer_email text not null, status text not null, total_cents integer "
                "not null, created_at timestamptz not null default now()) has 40 million "
                "rows. Write a zero-downtime migration that: introduces a customers table "
                "(id bigserial, email citext unique not null, created_at timestamptz), "
                "backfills it from distinct orders.customer_email, adds orders.customer_id "
                "bigint referencing customers(id), backfills it in batches of 10,000 "
                "without long locks, converts status to an enum type order_status with "
                "values pending, paid, shipped, cancelled, and adds an index on "
                "(customer_id, created_at desc) concurrently. Provide the forward "
                "migration as ordered SQL steps (marking which cannot run inside a "
                "transaction), a matching rollback migration that restores the original "
                "schema without data loss, and a short verification query for each step."
            ),
        },
        {
            "id": "typed-refactor",
            "prompt": (
                "Refactor this Python function into small, fully typed, testable units. "
                "Preserve behaviour exactly (including the order of the returned list and "
                "the handling of malformed rows), use dataclasses and typing (no Any), "
                "replace the magic numbers with named constants, and explain each "
                "behaviour-preserving decision briefly after the code.\n\n"
                "```python\n"
                "def process(rows, cfg=None):\n"
                "    out = []\n"
                "    seen = {}\n"
                "    cfg = cfg or {}\n"
                "    for r in rows:\n"
                "        try:\n"
                "            uid = int(r.get('user') or r['uid'])\n"
                "        except Exception:\n"
                "            continue\n"
                "        amt = float(r.get('amount', 0)) * (1.2 if r.get('region') == 'EU' else 1)\n"
                "        if cfg.get('skip_small') and amt < 5:\n"
                "            continue\n"
                "        if uid in seen:\n"
                "            seen[uid]['total'] += amt\n"
                "            seen[uid]['n'] += 1\n"
                "        else:\n"
                "            seen[uid] = {'uid': uid, 'total': amt, 'n': 1}\n"
                "            out.append(seen[uid])\n"
                "    for s in out:\n"
                "        s['avg'] = round(s['total'] / s['n'], 2)\n"
                "        if s['total'] > cfg.get('vip', 1000):\n"
                "            s['tier'] = 'vip'\n"
                "        elif s['total'] > 100:\n"
                "            s['tier'] = 'regular'\n"
                "        else:\n"
                "            s['tier'] = 'new'\n"
                "    return sorted(out, key=lambda s: -s['total'])\n"
                "```"
            ),
        },
    ]


def one_request(args, think_close: list[int], task: dict, seed: int) -> dict:
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": task["prompt"]}],
        "chat_template_kwargs": {"enable_thinking": True},
        "temperature": args.temperature,
        "top_p": args.top_p,
        "seed": seed,
        "max_tokens": args.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "return_token_ids": True,
    }
    if args.reasoning_effort:
        payload["reasoning_effort"] = args.reasoning_effort
    result = common.stream(args.base_url, "/v1/chat/completions", payload, args.timeout)
    timing = common.token_timing(result)
    ids = [token for chunk in result["chunks"] for token in chunk["token_ids"]]
    # Split on the </think> token sequence; the marker counts as reasoning.
    split = None
    width = len(think_close)
    for index in range(len(ids) - width + 1):
        if ids[index:index + width] == think_close:
            split = index + width
            break
    reasoning_tokens = len(ids) if split is None else split
    usage = result["usage"] or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "task": task["id"],
        "seed": seed,
        "finish_reason": result["finish_reason"],
        "ttft_seconds": timing["ttft_seconds"],
        "reasoning_tokens": reasoning_tokens,
        "content_tokens": len(ids) - reasoning_tokens,
        "reasoning_closed": split is not None,
        "completion_tokens": usage.get("completion_tokens"),
        "usage_reasoning_tokens": details.get("reasoning_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "decode_tokens": timing["decode_tokens"],
        "decode_seconds": timing["decode_seconds"],
        "decode_tokens_per_second": timing["n_minus_one_tps"],
        "post_burst_decode_tokens_per_second": timing["post_burst_tps"],
        "token_count_matches_usage": timing["token_count_matches_usage"],
        "reasoning_chars": len(result["reasoning"]),
        "content_chars": len(result["content"]),
        "content_preview": result["content"][:400],
        "reasoning_preview": result["reasoning"][:400],
        "started_perf_seconds": result["started_perf_seconds"],
        "first_token_offset_seconds": timing.get("first_token_offset_seconds"),
        "last_token_offset_seconds": timing.get("last_token_offset_seconds"),
        "request_seconds": result["request_seconds"],
        **({"content": result["content"], "reasoning": result["reasoning"]} if args.keep_text else {}),
    }


def batch(args, think_close, tasks, concurrency: int, batch_index: int) -> dict:
    barrier = threading.Barrier(concurrency)
    offset = batch_index * concurrency

    def worker(slot: int) -> dict:
        task = tasks[(offset + slot) % len(tasks)]
        seed = args.seed + 10_000 * concurrency + 100 * (batch_index + 1) + slot
        barrier.wait()
        return one_request(args, think_close, task, seed)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        requests = list(pool.map(worker, range(concurrency)))
    starts = [r["started_perf_seconds"] + r["first_token_offset_seconds"]
              for r in requests if r["first_token_offset_seconds"] is not None]
    ends = [r["started_perf_seconds"] + r["last_token_offset_seconds"]
            for r in requests if r["last_token_offset_seconds"] is not None]
    window = max(ends) - min(starts) if starts and ends else None
    tokens = sum(r["decode_tokens"] for r in requests)
    events = []
    for r in requests:
        if r["first_token_offset_seconds"] is not None:
            events += [(r["started_perf_seconds"] + r["first_token_offset_seconds"], 1),
                       (r["started_perf_seconds"] + r["last_token_offset_seconds"], -1)]
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return {
        "concurrency": concurrency,
        "batch": batch_index,
        "aggregate_decode_tokens": tokens,
        "aggregate_decode_seconds": window,
        "aggregate_decode_tokens_per_second": tokens / window if window else None,
        "sum_of_request_rates": sum(r["decode_tokens_per_second"] or 0 for r in requests),
        "peak_overlapping_streams": peak,
        "requests": requests,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="server root or .../v1")
    parser.add_argument("--model", required=True, help="served model name")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--warmup-batches", type=int, default=1)
    parser.add_argument("--batches", type=int, default=3, help="measured batches per concurrency")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=None,
                        help="omit to use the chat template default (max)")
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--no-spec-metrics", action="store_true",
                        help="skip /metrics spec-decode counter deltas")
    parser.add_argument("--metrics-wait", type=float, default=2.0,
                        help="idle seconds before each /metrics read (outside timed requests)")
    parser.add_argument("--inter-batch-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--keep-text", action="store_true", help="retain full reasoning/content text")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.concurrency) < 1 or args.batches < 1 or args.warmup_batches < 0 or args.max_tokens < 2:
        parser.error("require concurrency >= 1, batches >= 1, warmup-batches >= 0, max-tokens >= 2")
    args.base_url = common.server_root(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")

    tasks = prompts()
    think_close = common.tokenize(args.base_url, args.model, "</think>")
    points = []
    for concurrency in args.concurrency:
        for warmup in range(args.warmup_batches):
            batch(args, think_close, tasks, concurrency, -1 - warmup)
            time.sleep(args.inter_batch_seconds)
        batches = []
        for index in range(args.batches):
            before = None if args.no_spec_metrics else common.spec_snapshot(args.base_url, args.metrics_wait)
            result = batch(args, think_close, tasks, concurrency, index)
            after = None if args.no_spec_metrics else common.spec_snapshot(args.base_url, args.metrics_wait)
            result["spec_decode"] = common.spec_delta(before, after)
            batches.append(result)
            rates = [r["decode_tokens_per_second"] for r in result["requests"]]
            print(json.dumps({
                "concurrency": concurrency, "batch": index,
                "per_request_tps": [round(r, 2) if r else None for r in rates],
                "aggregate_tps": result["aggregate_decode_tokens_per_second"],
                "finish": [r["finish_reason"] for r in result["requests"]],
                "acceptance": (result["spec_decode"] or {}).get("acceptance_rate"),
            }), flush=True)
            time.sleep(args.inter_batch_seconds)
        requests = [r for b in batches for r in b["requests"]]
        spec_totals = [b["spec_decode"] for b in batches if b["spec_decode"] and "error" not in b["spec_decode"]]
        drafted = sum(s["draft_tokens"] for s in spec_totals)
        accepted = sum(s["accepted_tokens"] for s in spec_totals)
        drafts = sum(s["drafts"] for s in spec_totals)
        finish: dict[str, int] = {}
        for r in requests:
            finish[str(r["finish_reason"])] = finish.get(str(r["finish_reason"]), 0) + 1
        points.append({
            "concurrency": concurrency,
            "per_request_decode_tokens_per_second": common.spread([r["decode_tokens_per_second"] for r in requests]),
            "aggregate_decode_tokens_per_second": common.spread([b["aggregate_decode_tokens_per_second"] for b in batches]),
            "ttft_seconds": common.spread([r["ttft_seconds"] for r in requests]),
            "reasoning_tokens": common.spread([r["reasoning_tokens"] for r in requests]),
            "content_tokens": common.spread([r["content_tokens"] for r in requests]),
            "completion_tokens": common.spread([r["completion_tokens"] for r in requests]),
            "finish_reasons": finish,
            "spec_decode": {
                "drafts": drafts, "draft_tokens": drafted, "accepted_tokens": accepted,
                "acceptance_rate": accepted / drafted if drafted else None,
                "mean_acceptance_length": 1 + accepted / drafts if drafts else None,
            } if spec_totals else None,
            "batches": batches,
        })

    medians = {p["concurrency"]: p["per_request_decode_tokens_per_second"]["median"]
               for p in points if p["per_request_decode_tokens_per_second"]}
    score = (statistics.fmean(medians[c] for c in SCORE_CONCURRENCIES)
             if all(c in medians for c in SCORE_CONCURRENCIES) else None)
    report = {
        "schema": "glm53-reasoning-coding.v1",
        "model": args.model,
        "reasoning_coding_score": score,
        "score_definition": "mean of the C1, C2 and C4 median per-request decode tokens/s",
        "score_components": {f"C{c}": medians.get(c) for c in SCORE_CONCURRENCIES},
        "method": (
            "chat completions, thinking enabled (template default effort unless "
            "reasoning_effort is set), fixed per-request seeds; C simultaneous independent "
            "clients per batch released by a barrier; per-request decode = (N-1) tokens over "
            "that request's first-to-last streamed token window, counting reasoning and "
            "content tokens; aggregate = sum(N-1) over the batch's global first-to-last "
            "window; reasoning/content split at the </think> token; prompts repeat across "
            "batches so prefix caching may shorten TTFT; spec-decode deltas bracket each "
            "measured batch with an idle metrics wait outside the timed window"
        ),
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "tasks": tasks,
        "think_close_token_ids": think_close,
        "points": points,
    }
    with args.output.open("x") as destination:
        destination.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"reasoning_coding_score": score, "components": report["score_components"]}), flush=True)


if __name__ == "__main__":
    main()
