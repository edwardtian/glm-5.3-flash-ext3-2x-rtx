#!/usr/bin/env python3
"""Tabulate reasoning-coding score, acceptance and KV pool across dev arms."""
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "benchmarks/v0.8.0-dev")
rows = []
for arm in sorted(p for p in root.iterdir() if p.is_dir()):
    f = arm / "reasoning-coding.json"
    if not f.exists():
        continue
    d = json.loads(f.read_text())
    comps = d.get("score_components") or d.get("components") or {}
    acc, alen = [], []
    for point in d.get("points", []):
        sd = point.get("spec_decode") or {}
        if sd.get("acceptance_rate") is not None:
            acc.append(sd["acceptance_rate"])
        if sd.get("mean_acceptance_length") is not None:
            alen.append(sd["mean_acceptance_length"])
    if not acc:
        acc = [float(x) for x in re.findall(r'"acceptance(?:_rate)?":\s*([0-9.]+)', f.read_text())]
    pool = ""
    log = arm / "server.log"
    if log.exists():
        m = re.findall(r"GPU KV cache size: ([0-9,]+) tokens, Maximum concurrency for ([0-9,]+)", log.read_text(errors="ignore"))
        if m:
            pool = f"{m[-1][0]} @ {m[-1][1]}"
    rows.append((arm.name, d.get("reasoning_coding_score"), comps.get("C1"), comps.get("C2"), comps.get("C4"),
                 sum(acc) / len(acc) if acc else None, sum(alen) / len(alen) if alen else None, pool))
print(f"{'arm':34s} {'score':>7s} {'C1':>7s} {'C2':>7s} {'C4':>7s} {'accept':>7s} {'len':>5s}  pool")
for r in rows:
    fmt = lambda v, p=1: "-" if v is None else f"{v:.{p}f}"
    print(f"{r[0]:34s} {fmt(r[1]):>7s} {fmt(r[2]):>7s} {fmt(r[3]):>7s} {fmt(r[4]):>7s} {fmt(r[5],3):>7s} {fmt(r[6],2):>5s}  {r[7]}")
