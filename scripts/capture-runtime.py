#!/usr/bin/env python3
"""Save the serving container configuration, GPU state and startup memory ledger.

Records docker inspect (image, command/serving args, selected environment),
nvidia-smi GPU inventory (power limits, clocks, driver), per-process GPU
memory, and the vLLM startup memory lines per worker ("Model loading took",
"Memory profiling takes", "Available KV cache memory", "GPU KV cache size",
graph capture) plus every placement-ledger line (logger containing
"glm53-placement" or text "layer owner").  Read-only: it never starts, stops
or modifies containers.  summarize-results.py reads the resulting runtime.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import subprocess
import urllib.request
from pathlib import Path

DEFAULT_CONTAINER = os.environ.get("CONTAINER_NAME", "glm53-flash-exl3-b12x-vllm")
ENV_PREFIXES = ("VLLM_", "B12X_", "NCCL_", "CUDA_", "KV_FP8_", "GLM53_", "PYTORCH_", "HF_", "TORCH_")
GPU_FIELDS = (
    "index", "name", "uuid", "pci.bus_id", "driver_version", "vbios_version",
    "memory.total", "memory.used", "power.limit", "power.default_limit",
    "power.max_limit", "enforced.power.limit", "clocks.current.sm",
    "clocks.current.memory", "clocks.max.sm", "clocks.max.memory",
    "clocks.applications.graphics", "clocks.applications.memory", "pstate",
    "temperature.gpu", "persistence_mode", "pcie.link.gen.current", "pcie.link.width.current",
)
GPU_FIELDS_MINIMAL = ("index", "name", "uuid", "pci.bus_id", "driver_version",
                      "memory.total", "memory.used", "power.limit", "clocks.current.sm",
                      "clocks.current.memory", "clocks.max.sm", "clocks.max.memory")
MARKERS = (
    "Model loading took", "Memory profiling takes", "Available KV cache memory",
    "GPU KV cache size", "Maximum concurrency for", "reserved for KV Cache",
    "Graph capturing finished", "CUDA graph", "torch.compile takes", "Loading weights took",
    "Init engine", "init engine", "startup warmup complete",
)
PLACEMENT = re.compile(r"glm53-placement|layer[ _-]owner", re.IGNORECASE)
PROCESS = re.compile(r"\((?P<proc>[A-Za-z][A-Za-z0-9_]*) pid=(?P<pid>\d+)\)")
PATTERNS = {
    "model_loading": re.compile(
        r"Model loading took (?P<gib>[\d.]+) ?GiB(?: memory)?(?: and (?P<seconds>[\d.]+) ?s)?"),
    "memory_profiling_seconds": re.compile(r"Memory profiling takes (?P<seconds>[\d.]+) ?s"),
    "available_kv_cache_gib": re.compile(r"Available KV cache memory: (?P<gib>[\d.]+) ?GiB"),
    "gpu_kv_cache_tokens": re.compile(r"GPU KV cache size: (?P<tokens>[\d,]+) tokens"),
    "max_concurrency": re.compile(
        r"Maximum concurrency for (?P<request>[\d,]+) tokens per request: (?P<x>[\d.]+)x"),
    "graph_capture": re.compile(
        r"Graph capturing finished in (?P<seconds>[\d.]+) secs?, took (?P<gib>[-\d.]+) ?GiB"),
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def parse_serving_args(argv: list[str]) -> dict:
    parsed: dict = {"positional": []}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("--"):
            key, eq, value = item[2:].partition("=")
            if eq:
                parsed[key] = value
            elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                parsed[key] = argv[index + 1]
                index += 1
            else:
                parsed[key] = True
        else:
            parsed["positional"].append(item)
        index += 1
    for key in ("speculative-config", "limit-mm-per-prompt", "compilation-config", "profiler-config"):
        if isinstance(parsed.get(key), str):
            try:
                parsed[key] = json.loads(parsed[key])
            except json.JSONDecodeError:
                pass
    return parsed


def gpu_inventory() -> dict:
    for fields in (GPU_FIELDS, GPU_FIELDS_MINIMAL):
        result = run("nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits",
                     check=False)
        if result.returncode == 0:
            rows = []
            for line in result.stdout.splitlines():
                values = [value.strip() for value in line.split(",")]
                if len(values) == len(fields):
                    rows.append(dict(zip(fields, values)))
            break
    else:
        return {"error": result.stderr.strip()}
    header = run("nvidia-smi", check=False).stdout
    versions = {
        "driver": (re.search(r"Driver Version:\s*(\S+)", header) or [None, None])[1],
        "cuda": (re.search(r"CUDA Version:\s*(\S+)", header) or [None, None])[1],
    }
    apps = run("nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
               "--format=csv,noheader,nounits", check=False)
    processes = []
    if apps.returncode == 0:
        for line in apps.stdout.splitlines():
            values = [value.strip() for value in line.split(",")]
            if len(values) == 4:
                processes.append(dict(zip(("gpu_uuid", "pid", "process_name", "used_memory_mib"), values)))
    return {"gpus": rows, "versions": versions, "compute_processes": processes}


def json_tail(line: str):
    start = line.find("{")
    if start < 0:
        return None
    try:
        return json.loads(line[start:])
    except json.JSONDecodeError:
        return None


def memory_ledger(lines: list[str]) -> dict:
    workers: dict[str, dict] = {}
    for line in lines:
        process = PROCESS.search(line)
        label = process["proc"] if process else "main"
        entry = workers.setdefault(label, {})
        if process:
            entry["pid"] = int(process["pid"])
            rank = re.search(r"TP(\d+)", label)
            if rank:
                entry["tp_rank"] = int(rank.group(1))
        for name, pattern in PATTERNS.items():
            match = pattern.search(line)
            if not match:
                continue
            values = {key: value for key, value in match.groupdict().items() if value is not None}
            converted = {key: (int(value.replace(",", "")) if key in ("tokens", "request")
                               else float(value)) for key, value in values.items()}
            entry[name] = converted if len(converted) > 1 else next(iter(converted.values()))
        if PLACEMENT.search(line):
            entry.setdefault("placement_lines", []).append(line)
            parsed = json_tail(line)
            if parsed is not None:
                entry.setdefault("placement_records", []).append(parsed)
    return {label: entry for label, entry in workers.items() if set(entry) - {"pid", "tp_rank"}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--container", default=DEFAULT_CONTAINER,
                        help=f"container name or id (default $CONTAINER_NAME or {DEFAULT_CONTAINER})")
    parser.add_argument("--base-url", default=None,
                        help="optional server root; records /v1/models and /version when reachable")
    parser.add_argument("--log-output", type=Path, default=None,
                        help="optional path to save the complete container log")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")

    info = json.loads(run("docker", "inspect", args.container).stdout)[0]
    logs = run("docker", "logs", args.container)
    log_text = logs.stdout + logs.stderr
    lines = log_text.splitlines()
    command = (info["Config"].get("Cmd") or []) or info.get("Args") or []
    environment = {}
    for item in info["Config"].get("Env") or []:
        name, sep, value = item.partition("=")
        if sep and name.startswith(ENV_PREFIXES):
            environment[name] = value

    api = {}
    if args.base_url:
        root = args.base_url.rstrip("/").removesuffix("/v1")
        for name, path in (("models", "/v1/models"), ("version", "/version")):
            try:
                with urllib.request.urlopen(root + path, timeout=30) as response:
                    api[name] = json.load(response)
            except (OSError, ValueError) as exc:
                api[name] = {"error": str(exc)}

    receipt = {
        "schema": "glm53-runtime-v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "host": platform.node(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "container": args.container,
        "container_id": info["Id"],
        "state": {key: info["State"].get(key) for key in ("Status", "StartedAt", "Health") if key in info["State"]},
        "image_reference": info["Config"].get("Image"),
        "image_id": info["Image"],
        "image_labels": info["Config"].get("Labels") or {},
        "entrypoint": info["Config"].get("Entrypoint"),
        # "args" is the vLLM serving argv (model path first); kept under the
        # Qwen recipe's key name so the two summarizers read the same field.
        "args": command,
        "docker_args": info.get("Args"),
        "serving_args": parse_serving_args(command),
        "device_requests": info["HostConfig"].get("DeviceRequests"),
        "shm_size": info["HostConfig"].get("ShmSize"),
        "mounts": [{"source": m.get("Source"), "destination": m.get("Destination"), "mode": m.get("Mode")}
                   for m in info.get("Mounts") or []],
        "selected_environment": environment,
        "gpu": gpu_inventory(),
        "memory_ledger": memory_ledger(lines),
        "placement_lines": [line for line in lines if PLACEMENT.search(line)],
        "selected_startup_lines": [line for line in lines if any(marker in line for marker in MARKERS)],
        "log_line_count": len(lines),
        "api": api,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as destination:
        json.dump(receipt, destination, indent=2)
        destination.write("\n")
    if args.log_output is not None:
        with args.log_output.open("x") as destination:
            destination.write(log_text)
    print(json.dumps({"output": str(args.output), "workers": sorted(receipt["memory_ledger"]),
                      "placement_lines": len(receipt["placement_lines"])}))


if __name__ == "__main__":
    main()
