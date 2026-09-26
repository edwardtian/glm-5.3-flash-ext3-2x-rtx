#!/usr/bin/env python3
"""Summarize GPU kernel time in a vLLM torch-profiler trace by category.

Reads a Chrome-trace JSON (optionally gzipped) written by vLLM's
/start_profile endpoint and reports, per category, the summed kernel
duration, its share of busy GPU time and the top kernels. The categories are
name heuristics for the GLM-5.3 serving path; unmatched kernels are listed
under "other" so nothing is silently dropped.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path

CATEGORIES: list[tuple[str, str]] = [
    ("allreduce_comm", r"allreduce|all_reduce|oneshot|twoshot|pcie|nccl|a2a|dcp|peer|barrier|exchange|all_gather|reduce_scatter"),
    ("routed_moe", r"trellis|moe|expert|ep_route|w4a16|grouped|route"),
    ("indexer", r"indexer|mqa_logits|kpool|topk|top_k|paged_index|dsa"),
    ("sparse_mla", r"sparse_mla|mla|flash_mla|decode_split|split_merge|combine_lse"),
    ("kda", r"kda|gdn|delta|recurrent|fla_|chunk_|fused_recurrent|conv1d|causal_conv"),
    ("mhc", r"mhc|sinkhorn|hc_pre|hc_post|hyper"),
    ("dense_gemm", r"gemm|gemv|cutlass|nvjet|sm80_|sm90_|sm100_|sm120_|ampere|xmma|cublas|matmul|bf16"),
    ("sampling", r"sampl|softmax|argmax|rejection|multinomial|gumbel|logit"),
    ("norm_elementwise", r"rms|norm|elementwise|vectorized|reduce|copy|cat|index|fill|silu|act_and_mul|triton"),
]


def load(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    trace = load(args.trace)
    events = trace["traceEvents"] if isinstance(trace, dict) else trace
    kernels = [
        e
        for e in events
        if e.get("ph") == "X" and e.get("cat") in {"kernel", "gpu_memcpy", "gpu_memset"}
    ]
    if not kernels:
        raise SystemExit("no GPU kernel events in trace")
    compiled = [(name, re.compile(pattern, re.I)) for name, pattern in CATEGORIES]
    totals: dict[str, float] = defaultdict(float)
    per_kernel: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, int] = defaultdict(int)
    for event in kernels:
        name = event.get("name", "")
        category = "memcpy_memset" if event.get("cat") != "kernel" else "other"
        if category == "other":
            for cat_name, pattern in compiled:
                if pattern.search(name):
                    category = cat_name
                    break
        dur = float(event.get("dur", 0.0))
        totals[category] += dur
        per_kernel[category][name[:120]] += dur
        counts[category] += 1
    start = min(float(e["ts"]) for e in kernels)
    end = max(float(e["ts"]) + float(e.get("dur", 0.0)) for e in kernels)
    busy = sum(totals.values())
    summary = {
        "trace": str(args.trace),
        "window_ms": round((end - start) / 1000.0, 3),
        "busy_ms": round(busy / 1000.0, 3),
        "utilization": round(busy / max(end - start, 1e-9), 4),
        "categories": {},
    }
    print(f"window {summary['window_ms']} ms, GPU busy {summary['busy_ms']} ms "
          f"({summary['utilization']*100:.1f}%)")
    for category, total in sorted(totals.items(), key=lambda kv: -kv[1]):
        top = sorted(per_kernel[category].items(), key=lambda kv: -kv[1])[: args.top]
        summary["categories"][category] = {
            "ms": round(total / 1000.0, 3),
            "share_of_busy": round(total / busy, 4),
            "launches": counts[category],
            "top": [{"name": n, "ms": round(t / 1000.0, 3)} for n, t in top],
        }
        print(f"{category:18s} {total/1000.0:10.3f} ms  {100*total/busy:5.1f}%  "
              f"{counts[category]:7d} launches")
        for name, t in top[:3]:
            print(f"    {t/1000.0:9.3f} ms  {name}")
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
