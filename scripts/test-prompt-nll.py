#!/usr/bin/env python3
"""Teacher-forced quality probe: mean negative log-likelihood of fixed text.

Sends fixed documents to /v1/completions with ``prompt_logprobs=0`` and
``max_tokens=1`` so the server scores every prompt token under the serving
stack (weights, KV cache format, attention path). The mean NLL per token is a
perplexity proxy: a numerically degraded stack raises it. Comparing two
servers on the identical token sequence isolates serving-path changes (for
example FP8 versus NVFP4 KV cache, or a kernel port) from sampling noise.

Documents are recipe files plus a long synthetic retrieval document, so the
probe covers prose, code and long-range attention without network access.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import json
import math
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def post(base: str, path: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


# Frozen document revision: the first commit containing all three files.
DOC_REVISION = "4df1873"


def _at_revision(path: str, revision: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{revision}:{path}"],
        check=True, capture_output=True, text=True,
    ).stdout


def documents(long_tokens_hint: int, revision: str = DOC_REVISION) -> dict[str, str]:
    docs = {
        "prose-provenance": _at_revision("PROVENANCE.md", revision)[:24000],
        "code-start-sh": _at_revision("start.sh", revision)[:24000],
        "code-benchmark": _at_revision("scripts/benchmark-reasoning-coding.py", revision)[:24000],
    }
    # Long document: numbered facts whose later lines refer back to earlier
    # ones, so accurate prediction needs long-range attention.
    lines = []
    for i in range(long_tokens_hint // 24):
        key = hashlib.sha256(str(i).encode()).hexdigest()[:8]
        ref = hashlib.sha256(str(i // 2).encode()).hexdigest()[:8]
        lines.append(f"Record {i}: code {key}; parent record {i // 2} has code {ref}.")
    docs["long-backref"] = "\n".join(lines)
    return docs


def score(base: str, model: str, text: str, timeout: float) -> dict:
    body = {
        "model": model,
        "prompt": text,
        "max_tokens": 1,
        "temperature": 0,
        "prompt_logprobs": 0,
    }
    started = time.perf_counter()
    result = post(base, "/v1/completions", body, timeout)
    elapsed = time.perf_counter() - started
    entries = result["choices"][0].get("prompt_logprobs") or result.get("prompt_logprobs")
    values = []
    for entry in entries or []:
        if not entry:
            continue
        # vLLM returns {token_id: {"logprob": x, ...}} with the prompt token first.
        best = next(iter(entry.values()))
        values.append(float(best["logprob"]))
    tokens = len(values)
    nll = -sum(values) / tokens if tokens else float("nan")
    return {"tokens": tokens, "mean_nll": nll, "ppl": math.exp(nll) if tokens else None,
            "seconds": round(elapsed, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--long-tokens", type=int, default=16000)
    parser.add_argument("--doc-revision", default=DOC_REVISION,
                        help="git revision the prose/code documents are read from")
    parser.add_argument("--label", default="")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = {}
    for name, text in documents(args.long_tokens, args.doc_revision).items():
        results[name] = score(args.base_url.rstrip("/"), args.model, text, args.timeout)
        print(json.dumps({"doc": name, **results[name]}), flush=True)
    total_tokens = sum(r["tokens"] for r in results.values())
    weighted = sum(r["mean_nll"] * r["tokens"] for r in results.values()) / total_tokens
    summary = {"label": args.label, "model": args.model, "doc_revision": args.doc_revision,
               "documents": results,
               "token_weighted_mean_nll": weighted}
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"label": args.label, "token_weighted_mean_nll": round(weighted, 5)}))


if __name__ == "__main__":
    main()
