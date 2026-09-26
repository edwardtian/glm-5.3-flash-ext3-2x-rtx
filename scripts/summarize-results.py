#!/usr/bin/env python3
"""Render RESULTS.md from one or more benchmark-suite.sh result directories.

    summarize-results.py --profile baseline=benchmarks/v0.8.0/a \\
                         --profile layer-split=benchmarks/v0.8.0/b \\
                         --output benchmarks/v0.8.0/RESULTS.md

Every number is read from the raw receipts in each directory (missing parts
are reported as not measured).  The first section is the reasoning-coding
decision metric; profiles are ranked by it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--profile", action="append", required=True, metavar="NAME=DIR",
                    help="profile label and result directory; repeat for each column")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--title", default="GLM-5.3 Flash serving measurements")
parser.add_argument("--omit-ranges", action="store_true", help="median-only tables")
args = parser.parse_args()
roots: dict[str, Path] = {}
for item in args.profile:
    name, sep, directory = item.partition("=")
    if not sep or not name or not directory:
        parser.error(f"--profile expects NAME=DIR, got {item!r}")
    if name in roots:
        parser.error(f"duplicate profile name {name!r}")
    if not Path(directory).is_dir():
        parser.error(f"not a directory: {directory}")
    roots[name] = Path(directory)
DASH = "—"
lines: list[str] = []


def read(root: Path, name: str):
    path = root / name
    if not path.is_file():
        return None
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return json.loads(path.read_text())


def timed(rows):
    return [r for r in rows or [] if r.get("record") == "measurement" and r.get("timed")]


def link(label: str, root: Path, name: str) -> str:
    if not (root / name).exists():
        return DASH
    return f"[{label}]({os.path.relpath(root / name, args.output.parent)})"


def table(headers, rows) -> None:
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    lines.append("")


def spread(values, digits: int = 2) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return DASH
    median = f"{statistics.median(values):.{digits}f}"
    if args.omit_ranges or len(values) == 1:
        return median
    return f"{median} ({min(values):.{digits}f}–{max(values):.{digits}f})"


def summary_spread(summary: dict | None, digits: int = 2) -> str:
    if not summary:
        return DASH
    median = f"{summary['median']:.{digits}f}"
    if args.omit_ranges or summary.get("n", 2) == 1:
        return median
    return f"{median} ({summary['min']:.{digits}f}–{summary['max']:.{digits}f})"


def pct(value) -> str:
    return DASH if value is None else f"{value * 100:.2f}%"


def num(value, digits: int = 2) -> str:
    return DASH if value is None else f"{value:.{digits}f}"


def raw_links(name: str, label: str | None = None) -> None:
    present = [link(label or profile, root, name) for profile, root in roots.items() if (root / name).exists()]
    if present:
        lines.extend(["Raw: " + ", ".join(present) + ".", ""])


def not_measured(section: str, name: str) -> bool:
    if all(not (root / name).exists() for root in roots.values()):
        lines.extend([f"_{section}: not measured in these result directories._", ""])
        return True
    return False


suites = {name: read(root, "suite.json") or {} for name, root in roots.items()}
runtimes = {name: read(root, "runtime.json") for name, root in roots.items()}
lines += [f"# {args.title}", "",
          "Generated from the linked raw receipts by `scripts/summarize-results.py`. "
          "Values are median (minimum–maximum) unless noted. Decode rates use the recipe's "
          "N−1 convention over each stream's first-to-last token window and exclude prefill "
          "(TTFT). Speculative acceptance comes from vLLM `/metrics` counter deltas.", ""]
if args.omit_ranges:
    lines[-2] = lines[-2].replace("Values are median (minimum–maximum) unless noted", "Values are medians")

# --------------------------------------------------------------------------
lines += ["## Reasoning-coding decision metric", "",
          "Thinking enabled at the template default effort, temperature 1.0, top-p 0.95, fixed "
          "per-request seeds, four rotating coding prompts (LRU+TTL cache with tests, async task-"
          "runner fix, PostgreSQL migration with rollback, typed refactor). Each concurrency runs "
          "one warmup batch and three measured batches of C simultaneous requests. Per-request "
          "decode counts all generated tokens (reasoning and content). The **score** is the mean "
          "of the C1, C2 and C4 per-request medians; tuning decisions are ranked by it.", ""]
reasoning = {name: read(root, "reasoning-coding.json") for name, root in roots.items()}
if not not_measured("Reasoning-coding", "reasoning-coding.json"):
    ranked = sorted((name for name in roots if reasoning[name]),
                    key=lambda name: -(reasoning[name].get("reasoning_coding_score") or 0))
    reference = next(iter(n for n in roots if reasoning[n] and reasoning[n].get("reasoning_coding_score")), None)
    rows = []
    for rank, name in enumerate(ranked, 1):
        report = reasoning[name]
        score = report.get("reasoning_coding_score")
        points = {p["concurrency"]: p for p in report["points"]}
        base = reasoning[reference]["reasoning_coding_score"] if reference else None
        delta = f"{(score / base - 1) * 100:+.1f}%" if score and base and name != reference else DASH
        spec = [points[c].get("spec_decode") for c in (1, 2, 4) if c in points and points[c].get("spec_decode")]
        drafted = sum(s["draft_tokens"] for s in spec)
        accepted = sum(s["accepted_tokens"] for s in spec)
        finish: dict[str, int] = {}
        for point in points.values():
            for reason, count in point["finish_reasons"].items():
                finish[reason] = finish.get(reason, 0) + count
        rows.append([rank, name, f"**{num(score)}**", delta] +
                    [summary_spread(points[c]["per_request_decode_tokens_per_second"]) if c in points else DASH
                     for c in (1, 2, 4)] +
                    [pct(accepted / drafted) if drafted else DASH,
                     ", ".join(f"{k} {v}" for k, v in sorted(finish.items())),
                     link("receipt", roots[name], "reasoning-coding.json")])
    table(["Rank", "Profile", "Score, tok/s", f"Δ vs {reference}", "C1 per-request", "C2 per-request",
           "C4 per-request", "Draft acceptance", "Finish reasons", "Raw"], rows)
    detail = []
    for name in ranked:
        for point in reasoning[name]["points"]:
            spec = point.get("spec_decode") or {}
            detail.append([name, point["concurrency"],
                           summary_spread(point["per_request_decode_tokens_per_second"]),
                           summary_spread(point["aggregate_decode_tokens_per_second"]),
                           summary_spread(point["ttft_seconds"], 3),
                           summary_spread(point["reasoning_tokens"], 0),
                           summary_spread(point["content_tokens"], 0),
                           pct(spec.get("acceptance_rate")), num(spec.get("mean_acceptance_length"), 3)])
    table(["Profile", "C", "Per-request tok/s", "Aggregate tok/s", "TTFT, s", "Reasoning tokens",
           "Content tokens", "Acceptance", "Mean accepted length"], detail)
    caps = sorted({reasoning[n]["args"].get("max_tokens") for n in ranked})
    lines += ["Aggregate divides the batch's summed N−1 tokens by its global first-to-last token "
              f"window. With a {'/'.join(map(str, caps))}-token cap many requests end inside reasoning "
              "(`length`); the rate "
              "is decode speed, not task success. Prompts repeat across batches, so TTFT can include "
              "prefix-cache hits.", ""]

# --------------------------------------------------------------------------
lines += ["## Profiles", ""]
profile_rows = []
for name, root in roots.items():
    runtime = runtimes[name] or {}
    suite = suites[name]
    serving = runtime.get("serving_args") or {}
    spec = serving.get("speculative-config") if isinstance(serving.get("speculative-config"), dict) else {}
    gpus = (runtime.get("gpu") or {}).get("gpus") or []
    gpu_text = DASH
    if gpus:
        names = sorted({g.get("name", "?") for g in gpus})
        limits = sorted({g.get("power.limit", "?") for g in gpus})
        gpu_text = f"{len(gpus)}× {'/'.join(names)} @ {'/'.join(limits)} W"
    method = spec.get("method") if spec else suite.get("spec_method", DASH)
    tokens = spec.get("num_speculative_tokens") if spec else suite.get("spec_tokens", DASH)
    profile_rows.append([
        name, suite.get("model", DASH), f"{method} K{tokens}" if method not in (None, DASH) else DASH,
        serving.get("max-model-len", suite.get("max_model_len", DASH)),
        serving.get("max-num-seqs", DASH), serving.get("gpu-memory-utilization", DASH),
        serving.get("kv-cache-dtype", suite.get("kv_cache_profile", DASH)),
        f"TP{serving.get('tensor-parallel-size', '?')}/DCP{serving.get('decode-context-parallel-size', '?')}"
        + ("/EP" if serving.get("enable-expert-parallel") else "") if serving else DASH,
        gpu_text, (runtime.get("image_id") or DASH)[:19],
        (suite.get("recipe_revision") or DASH)[:10] + ("+dirty" if suite.get("recipe_dirty") else ""),
        " ".join(item for item in (link("runtime", root, "runtime.json"), link("suite", root, "suite.json"))
                 if item != DASH) or DASH,
    ])
table(["Profile", "Served model", "Speculation", "Max model len", "Max seqs", "GPU memory fraction",
       "KV cache", "Parallelism", "GPUs", "Image", "Recipe", "Configuration"], profile_rows)

# --------------------------------------------------------------------------
lines += ["## Per-GPU placement and memory ledger", "",
          "From `runtime.json` (`capture-runtime.py`): vLLM startup memory lines per worker "
          "process, the GPU inventory at capture time, and every placement-ledger line "
          "(`glm53-placement` logger or `layer owner`).", ""]
if all(runtimes[name] is None for name in roots):
    lines += ["_No runtime.json in these result directories._", ""]
for name, root in roots.items():
    runtime = runtimes[name]
    if runtime is None:
        continue
    lines += [f"### {name}", ""]
    ledger = runtime.get("memory_ledger") or {}
    rows = []
    for worker, entry in sorted(ledger.items(), key=lambda kv: (kv[1].get("tp_rank", 99), kv[0])):
        loading = entry.get("model_loading")
        loading_gib = loading.get("gib") if isinstance(loading, dict) else loading
        loading_s = loading.get("seconds") if isinstance(loading, dict) else None
        capture = entry.get("graph_capture") if isinstance(entry.get("graph_capture"), dict) else {}
        kv_tokens = entry.get("gpu_kv_cache_tokens")
        concurrency = entry.get("max_concurrency") if isinstance(entry.get("max_concurrency"), dict) else {}
        rows.append([worker, entry.get("tp_rank", DASH), num(loading_gib), num(loading_s, 1),
                     num(entry.get("memory_profiling_seconds"), 1), num(entry.get("available_kv_cache_gib")),
                     num(capture.get("gib")), f"{kv_tokens:,}" if isinstance(kv_tokens, int) else DASH,
                     f"{concurrency['x']:.2f}× @ {concurrency['request']:,}" if concurrency else DASH,
                     len(entry.get("placement_lines") or [])])
    if rows:
        table(["Process", "TP rank", "Weights GiB", "Load s", "Profiling s", "Available KV GiB",
               "Graph GiB", "KV cache tokens", "Max concurrency", "Placement lines"], rows)
    else:
        lines += ["_No vLLM memory lines found in the container log._", ""]
    gpus = (runtime.get("gpu") or {}).get("gpus") or []
    if gpus:
        table(["GPU", "Name", "Memory used / total MiB", "Power limit W", "SM clock cur/max MHz",
               "Mem clock cur/max MHz", "App clocks gfx/mem MHz", "Driver", "PCIe"],
              [[g.get("index"), g.get("name"), f"{g.get('memory.used', '?')} / {g.get('memory.total', '?')}",
                g.get("power.limit"), f"{g.get('clocks.current.sm', '?')}/{g.get('clocks.max.sm', '?')}",
                f"{g.get('clocks.current.memory', '?')}/{g.get('clocks.max.memory', '?')}",
                f"{g.get('clocks.applications.graphics', '?')}/{g.get('clocks.applications.memory', '?')}",
                g.get("driver_version"),
                f"Gen{g.get('pcie.link.gen.current', '?')} x{g.get('pcie.link.width.current', '?')}"]
               for g in gpus])
    placement = runtime.get("placement_lines") or []
    if placement:
        shown = placement[:60]
        lines += [f"Placement ledger ({len(placement)} lines" + (", first 60 shown" if len(placement) > 60 else "") + "):",
                  "", "```text", *shown, "```", ""]
    else:
        lines += ["No placement-ledger lines were logged.", ""]

# --------------------------------------------------------------------------
lines += ["## Seven content workloads: C1", "",
          "One warmup and three measured responses per workload, temperature zero, thinking "
          "closed at render time. The weighted blend is total N−1 tokens divided by total decode "
          "time. Rates include failed output contracts and are not successful-task throughput.", ""]
if not not_measured("Seven workloads", "seven.jsonl"):
    seven = {name: read(root, "seven.jsonl") for name, root in roots.items()}
    cases = ("code", "math", "fable", "hello", "topic", "structured-json", "multilingual")
    rows = []
    for case in cases:
        row = [case]
        for name in roots:
            samples = [r for r in timed(seven[name]) if r["case"] == case]
            passed = sum(bool(r["contract"].get("quality_contract_passed")) for r in samples)
            row += [spread([r["decode_tps"] for r in samples]), f"{passed}/{len(samples)}" if samples else DASH]
        rows.append(row)
    table(["Workload"] + [h for n in roots for h in (n + " tok/s", "Contract")], rows)
    rows = []
    for name, root in roots.items():
        samples = timed(seven[name])
        seconds = sum(r["decode_seconds"] or 0 for r in samples)
        after = next((r for r in seven[name] or [] if r.get("record") == "spec_after"), None)
        delta = (after or {}).get("timed_suite_delta") or {}
        rows.append([name, num(sum(r["decode_tokens"] for r in samples) / seconds if seconds else None),
                     pct(delta.get("acceptance_rate")), num(delta.get("mean_acceptance_length"), 3),
                     link("responses and timings", root, "seven.jsonl")])
    table(["Profile", "Weighted blend", "Draft acceptance", "Mean acceptance length", "Raw"], rows)
    lines += ["The code contract checks syntax and required assertions without executing code; "
              "the Chinese terminology check is a literal-phrase proxy. Rejected responses remain "
              "in the raw files.", ""]

lines += ["## Orchid repetition: C1", "",
          "Exactly 100 space-separated `orchid` words requested with a 1500-token cap; one warmup "
          "and five measured runs. A fast incorrect repetition is not a task success.", ""]
if not not_measured("Orchid", "orchid.jsonl"):
    rows = []
    for name, root in roots.items():
        samples = timed(read(root, "orchid.jsonl"))
        if not samples:
            continue
        rows.append([name, ", ".join(str(r["contract"]["occurrences"]) for r in samples),
                     f"{sum(r['contract']['pass'] for r in samples)}/{len(samples)}",
                     spread([r["decode_tps"] for r in samples]), link("responses", root, "orchid.jsonl")])
    table(["Profile", "Word counts", "Exact contract", "Decode tok/s", "Raw"], rows)

lines += ["## Sampled prose: independent clients", "",
          "Each client requests 256 forced output tokens at temperature 0.7 with fixed per-client "
          "seeds and thinking closed at render time. Two full warmups and three measurements per "
          "concurrency. Aggregate rate divides the summed N−1 tokens by the batch's first-to-last "
          "token window; overlap counts come from client stream intervals.", ""]
if not not_measured("Clients", "clients.json"):
    clients = {name: read(root, "clients.json") for name, root in roots.items()}
    levels = sorted({p["concurrency"] for c in clients.values() if c for p in c["points"]})
    rows = []
    for level in levels:
        row = [level]
        for name in roots:
            point = next((p for p in (clients[name] or {}).get("points", []) if p["concurrency"] == level), None)
            if point is None:
                row += [DASH, DASH, DASH]
                continue
            runs = point["runs"]
            overlap = [r.get("peak_overlapping_stream_intervals") for r in runs if "peak_overlapping_stream_intervals" in r]
            spec = [r["spec_decode"] for r in runs if r.get("spec_decode") and "error" not in r["spec_decode"]]
            drafted = sum(s["draft_tokens"] for s in spec)
            row += [spread([r["decode_tokens_per_second"] for r in runs]),
                    (f"{min(overlap)}" if min(overlap) == max(overlap) or args.omit_ranges else f"{min(overlap)}–{max(overlap)}") if overlap else DASH,
                    pct(sum(s["accepted_tokens"] for s in spec) / drafted) if drafted else DASH]
        rows.append(row)
    table(["Clients"] + [h for n in roots for h in (n + " aggregate tok/s", "Overlap", "Acceptance")], rows)
    raw_links("clients.json")

lines += ["## Prefill matrix: C1", "",
          "Exact prompt lengths, unique first cache blocks, three measurements after a warmup at "
          "each depth. Effective prompt tok/s includes server tokenization and the first output "
          "token handoff; it is not isolated GPU prefill time.", ""]
if not not_measured("Prefill", "prefill.json"):
    prefill = {name: read(root, "prefill.json") for name, root in roots.items()}
    depths = sorted({p["prompt_tokens"] for r in prefill.values() if r for p in r["points"]})
    rows = []
    for depth in depths:
        row = [f"{depth:,}"]
        for name in roots:
            point = next((p for p in (prefill[name] or {}).get("points", []) if p["prompt_tokens"] == depth), None)
            row += ([spread([r["effective_prompt_tokens_per_second"] for r in point["runs"]], 1),
                     spread([r["ttft_seconds"] for r in point["runs"]], 3)] if point else [DASH, DASH])
        rows.append(row)
    table(["Prompt tokens"] + [h for n in roots for h in (n + " tok/s", n + " TTFT, s")], rows)
    raw_links("prefill.json")

lines += ["## Context and decode scaling: C1", "",
          "Exact-length synthetic filler followed by 256 forced output tokens; one warmup and "
          "three measurements per depth. Serving capacity and speed, not long-context quality.", ""]
if not not_measured("Context", "context.jsonl"):
    context = {name: timed(read(root, "context.jsonl")) for name, root in roots.items()}
    depths = sorted({r["depth"] for rows_ in context.values() for r in rows_})
    rows = []
    for depth in depths:
        row = [f"{depth:,}"]
        for name in roots:
            samples = [r for r in context[name] if r["depth"] == depth]
            accepted = [r["spec_decode"] for r in samples if r.get("spec_decode") and "error" not in r["spec_decode"]]
            drafted = sum(s["draft_tokens"] for s in accepted)
            row += [spread([r["decode_tps"] for r in samples]), spread([r["ttft_seconds"] for r in samples], 3),
                    pct(sum(s["accepted_tokens"] for s in accepted) / drafted) if drafted else DASH]
        rows.append(row)
    table(["Prompt tokens"] + [h for n in roots for h in (n + " decode tok/s", n + " TTFT, s", "Acceptance")], rows)
    raw_links("context.jsonl")
for name, root in roots.items():
    boundary = timed(read(root, "context-boundary.jsonl"))
    if boundary:
        usage = boundary[0]["usage"]
        limit = suites[name].get("max_model_len")
        lines += [f"{name} returned {usage['completion_tokens']} of 256 requested tokens after a "
                  f"{usage['prompt_tokens']:,}-token prompt"
                  + (f" (configured max model length {limit:,})" if limit else "")
                  + f" at {num(boundary[0]['decode_tps'])} tok/s: {link('receipt', root, 'context-boundary.jsonl')}.", ""]

lines += ["## Reference coding task across retained KV depths: C1", "",
          "The async task-runner prompt, thinking closed, temperature 0.2, fixed seed, 256 forced "
          "output tokens; ordinary filler precedes the task. Prompts repeat, so these TTFTs are not "
          "uncached prefill. The token cap is not a code-correctness test.", ""]
if not not_measured("Code agent", "code-agent.json"):
    coding = {name: read(root, "code-agent.json") for name, root in roots.items()}
    depths = sorted({p["existing_depth_tokens"] for r in coding.values() if r for p in r["points"]})
    rows = []
    for depth in depths:
        row = ["Task only" if depth == 0 else f"{depth:,}"]
        for name in roots:
            point = next((p for p in (coding[name] or {}).get("points", []) if p["existing_depth_tokens"] == depth), None)
            row += ([spread([r["decode_tokens_per_second"] for r in point["runs"]]),
                     pct(point.get("median_accepted_draft_rate"))] if point else [DASH, DASH])
        rows.append(row)
    table(["Prompt depth"] + [h for n in roots for h in (n + " decode tok/s", "Acceptance")], rows)
    raw_links("code-agent.json")

# --------------------------------------------------------------------------
lines += ["## Functional and tool checks", ""]
rows = []
for name, root in roots.items():
    api = [r for r in read(root, "api-tools.jsonl") or [] if r.get("record") == "measurement"]
    vision = read(root, "vision.json")
    if vision is None:
        vision_text = DASH
    elif vision.get("skipped"):
        vision_text = f"skipped: {vision['skipped']}"
    else:
        supported = vision.get("supported_image_counts", {})
        vision_text = ", ".join(f"{n}: {'pass' if v['passed'] else 'fail'}" for n, v in supported.items())
        rejected = vision.get("seventeen_images_rejected")
        if rejected is not None:
            vision_text += f"; 17 rejected: {'pass' if rejected['passed'] else 'fail'}"
    retrieval = [r for r in read(root, "retrieval.jsonl") or [] if r.get("record") == "measurement"]
    by_depth: dict[int, list[bool]] = {}
    for r in retrieval:
        by_depth.setdefault(r["filler_tokens"], []).append(r["passed"])
    needle = read(root, "multi-needle.json")
    rows.append([
        name,
        f"{sum(r['passed'] for r in api)}/{len(api)}" if api else DASH,
        vision_text,
        ", ".join(f"{d:,}: {sum(v)}/{len(v)}" for d, v in sorted(by_depth.items())) or DASH,
        (f"{sum(c['passed'] for c in needle['checks'])}/{len(needle['checks'])} at "
         f"{needle['target_prompt_tokens']:,}") if needle else DASH,
    ])
table(["Profile", "API tool choices", "Numbered images per request", "Retrieval by filler tokens",
       "Multi-needle"], rows)
leaks = {name: sum(1 for r in read(root, "api-tools.jsonl") or []
                   if r.get("record") == "measurement" and not r["thinking"] and r.get("reasoning_chars"))
         for name, root in roots.items()}
if any(leaks.values()):
    lines += ["Thinking-off API requests that still returned reasoning text: "
              + ", ".join(f"{n} {c}" for n, c in leaks.items()) + ".", ""]
lines += ["API checks cover required/named/auto/none tool choices, thinking off/on via "
          "`chat_template_kwargs.enable_thinking`, and streaming/non-streaming. Retrieval places one "
          "random key early, midway and late in exact-length filler archives with thinking closed. "
          "Image checks read ordered numbers from 1, 4 and 16 images and require a 17-image "
          "rejection; they are smoke tests, not broad vision evaluation.", ""]
tool_rows = []
for name, root in roots.items():
    tools = read(root, "tools.json")
    if not tools:
        continue
    scores = tools["scores"]
    hard = next((c for c in scores["category_scores"] if c["label"] == "Hard Mode"), None)
    tool_rows.append([name, tools.get("status"), f"{scores['total_points']}/{scores['max_points']}",
                      f"{hard['earned']}/{hard['max']}" if hard else DASH,
                      link("full tool traces", root, "tools.md")])
if tool_rows:
    table(["Profile", "Status", "Full suite points", "Hard Mode points", "Raw"], tool_rows)
    for name, root in roots.items():
        flags = (read(root, "tools.json") or {}).get("safety_warnings") or []
        if flags:
            lines += [f"{name} evaluator-flagged cases:", ""] + ["- " + flag for flag in flags] + [""]
    lines += ["Tool-eval-bench is pinned at `cf54b4bfe705f12f71e8866f10730572497c8105`: all 88 cases "
              "including 19 Hard Mode, eight parallel cases (C8), temperature zero, template-default "
              "thinking, one trial, at most eight turns. Points are not a normalized cross-setup score.", ""]

failures = []
for name, root in roots.items():
    for step in read(root, "steps.jsonl") or []:
        if step["status"] != 0:
            failures.append([name, step["name"], step["status"], link("log", root, f"logs/{step['name']}.log")])
    skipped = root / "skipped.txt"
    if skipped.is_file():
        for entry in skipped.read_text().splitlines():
            failures.append([name, "skipped", DASH, entry])
if failures:
    lines += ["## Failed or skipped steps", ""]
    table(["Profile", "Step", "Exit status", "Detail"], failures)

args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text("\n".join(lines).rstrip() + "\n")
