#!/usr/bin/env python3
"""Stream the seven content workloads or orchid repeats at C1, thinking off.

Prompts are the glmrt contracts in test-content-vllm.py.  Each request is
rendered with thinking disabled (see glm53_bench.render_chat), prefixed by a
per-run nonce so prefix caching cannot reuse an earlier run, and streamed from
/v1/completions at temperature zero with token IDs for auditable timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from pathlib import Path

import glm53_bench as common

contracts = common.load_sibling("test-content-vllm.py", "glmrt_vllm_contracts")
ORCHID = contracts.PromptCase(
    "repeat",
    'Repeat only the single word "orchid" exactly 100 times, separated by spaces. '
    "Do not add any other text.",
    1500,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="server root or .../v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--suite", choices=("seven", "orchid"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--seed", type=int, default=787)
    parser.add_argument("--reasoning-effort", default="low",
                        help="effort line rendered with thinking closed (existing GLM content tests use low)")
    parser.add_argument("--collect-spec-metrics", action="store_true",
                        help="suite-level spec-decode counter delta; requires exclusive use of the server")
    parser.add_argument("--metrics-wait", type=float, default=2.0,
                        help="idle seconds before each /metrics read, outside timed requests")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0 or args.metrics_wait < 0:
        parser.error("runs must be positive and warmups/metrics-wait nonnegative")
    args.base_url = common.server_root(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = contracts.CASES if args.suite == "seven" else {"orchid": ORCHID}
    records = []
    before = None
    with args.output.open("x") as destination:
        destination.write(json.dumps({
            "record": "meta", "schema": "glm53-workloads-v1",
            "args": vars(args) | {"output": str(args.output)},
            "timing": "client SSE chunks; decode_tps = (N-1)/first-to-last token window; "
                      "post_burst_tps also excludes the first chunk; raw token IDs retained",
        }) + "\n")
        for run in range(-args.warmups, args.runs):
            if run == 0 and args.collect_spec_metrics:
                before = common.spec_snapshot(args.base_url, args.metrics_wait)
                destination.write(json.dumps({"record": "spec_before", **before}) + "\n")
                destination.flush()
            for name, case in cases.items():
                nonce = hashlib.sha256(f"{args.seed}:{run}:{name}".encode()).hexdigest()[:16]
                prompt = f"Nonce {nonce}; ignore this identifier.\n{case.prompt}"
                rendered = common.render_chat(
                    args.base_url, args.model, [{"role": "user", "content": prompt}],
                    thinking=False, reasoning_effort=args.reasoning_effort, timeout=args.timeout)
                result = common.stream(
                    args.base_url, "/v1/completions",
                    common.completion_payload(args.model, rendered["token_ids"], case.max_tokens,
                                              temperature=0, seed=args.seed),
                    args.timeout)
                timing = common.token_timing(result)
                if not timing["token_count_matches_usage"]:
                    raise RuntimeError(f"incomplete token timing evidence: {timing}, usage={result['usage']}")
                if name == "orchid":
                    words = result["content"].strip().split()
                    contract = {"pass": words == ["orchid"] * 100,
                                "occurrences": len(re.findall(r"\borchid\b", result["content"]))}
                else:
                    contract = contracts.validate_case_content(name, result["content"])
                row = {"record": "measurement", "run": run, "timed": run >= 0, "case": name,
                       "prompt": prompt, "max_tokens": case.max_tokens,
                       "thinking_control": rendered["method"],
                       "close_think_appended": rendered["close_think_appended"],
                       "contract": contract, "content": result["content"],
                       "usage": result["usage"], "finish_reason": result["finish_reason"],
                       "elapsed_seconds": result["request_seconds"],
                       "ttft_seconds": timing["ttft_seconds"],
                       "decode_seconds": timing["decode_seconds"],
                       "decode_tokens": timing["decode_tokens"],
                       "decode_tps": timing["n_minus_one_tps"],
                       "post_burst_tps": timing["post_burst_tps"],
                       "chunks": [{"seconds": c["seconds"], "token_ids": c["token_ids"]}
                                  for c in result["chunks"] if c["token_ids"]]}
                destination.write(json.dumps(row, ensure_ascii=False) + "\n")
                destination.flush()
                if run >= 0:
                    records.append(row)
                print(json.dumps({"run": run, "case": name,
                                  "passed": contract.get("pass", contract.get("quality_contract_passed")),
                                  "decode_tps": row["decode_tps"]}), flush=True)
        if args.collect_spec_metrics:
            after = common.spec_snapshot(args.base_url, args.metrics_wait)
            destination.write(json.dumps({"record": "spec_after", **after,
                                          "timed_suite_delta": common.spec_delta(before, after)}) + "\n")
        total_seconds = sum(row["decode_seconds"] or 0 for row in records)
        summary = {
            "record": "summary",
            "weighted_decode_tps": (sum(row["decode_tokens"] for row in records) / total_seconds
                                    if total_seconds else None),
            "median_tps_by_case": {
                name: (statistics.median(values) if values else None)
                for name in cases
                for values in [[row["decode_tps"] for row in records
                                if row["case"] == name and row["decode_tps"] is not None]]
            },
        }
        destination.write(json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
