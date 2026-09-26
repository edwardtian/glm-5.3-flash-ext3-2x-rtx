"""Shared stdlib-only helpers for the GLM-5.3 benchmark/qualification suite.

Imported by sibling scripts (Python puts the script directory on sys.path).

Thinking control: the served GLM-5.3 chat templates open ``<think>``
unconditionally and do not read ``enable_thinking``; vLLM's glm45 reasoning
parser does read it (it then routes all output to ``content``).  Thinking-off
workloads therefore render the chat through ``/v1/chat/completions/render``
with ``chat_template_kwargs={"enable_thinking": false}`` and, when the rendered
prompt still ends in an open ``<think>``, append ``</think>`` and send the
token IDs to ``/v1/completions``.  This is the same method as the existing
test-content-vllm.py and benchmark-dflash2-vllm.py; if a future template
honours ``enable_thinking`` nothing is appended.  Every receipt records which
path was taken.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent

SPEC_COUNTERS = {
    "vllm:spec_decode_num_drafts_total": "drafts",
    "vllm:spec_decode_num_draft_tokens_total": "draft_tokens",
    "vllm:spec_decode_num_accepted_tokens_total": "accepted_tokens",
}
SPEC_PER_POSITION = "vllm:spec_decode_num_accepted_tokens_per_pos_total"
_SAMPLE = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>\S+)")
_POSITION = re.compile(r'position="(\d+)"')


def load_sibling(filename: str, name: str | None = None) -> ModuleType:
    """Import a hyphenated sibling script as a module."""
    path = SCRIPT_DIR / filename
    module_name = name or "glm53_" + path.stem.replace("-", "_")
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def server_root(base_url: str) -> str:
    """Accept either http://host:port or http://host:port/v1."""
    return base_url.rstrip("/").removesuffix("/v1").rstrip("/")


def post_json(base_url: str, path: str, payload: dict, timeout: float = 3600) -> dict:
    request = urllib.request.Request(
        server_root(base_url) + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def tokenize(base_url: str, model: str, text: str, timeout: float = 600) -> list[int]:
    return post_json(
        base_url,
        "/tokenize",
        {"model": model, "prompt": text, "add_special_tokens": False},
        timeout,
    )["tokens"]


# --------------------------------------------------------------------------
# Thinking control


def render_chat(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    thinking: bool,
    reasoning_effort: str | None = None,
    timeout: float = 600,
) -> dict:
    """Render messages to prompt token IDs, closing <think> when thinking is off."""
    payload: dict = {
        "model": model,
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    rendered = post_json(base_url, "/v1/chat/completions/render", payload, timeout)
    ids = list(rendered["token_ids"])
    think_open = tokenize(base_url, model, "<think>", timeout)
    think_close = tokenize(base_url, model, "</think>", timeout)
    appended = False
    if not thinking:
        opened = ids[-len(think_open):] == think_open
        already_closed = ids[-len(think_open) - len(think_close):] == think_open + think_close
        if opened and not already_closed:
            ids += think_close
            appended = True
    return {
        "token_ids": ids,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "close_think_appended": appended,
        "think_open_ids": think_open,
        "think_close_ids": think_close,
        "method": (
            "render(enable_thinking=false) + appended </think>; /v1/completions"
            if appended
            else "render(enable_thinking=%s) unchanged; /v1/completions" % str(thinking).lower()
        ),
    }


# --------------------------------------------------------------------------
# Speculative-decoding counters


def spec_snapshot(base_url: str, wait_seconds: float = 0.0, timeout: float = 30) -> dict:
    """Read spec-decode counters from /metrics.

    ``available`` is False when the server exposes no spec-decode counters
    (speculation disabled) or /metrics is unreachable; callers then record
    ``None`` deltas instead of failing.
    """
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    try:
        with urllib.request.urlopen(server_root(base_url) + "/metrics", timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"available": False, "error": str(exc), "unix_seconds": time.time()}
    counters = {name: 0.0 for name in SPEC_COUNTERS.values()}
    per_position: dict[str, float] = {}
    seen = False
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            continue
        name = match["name"]
        if name in SPEC_COUNTERS:
            counters[SPEC_COUNTERS[name]] += float(match["value"])
            seen = True
        elif name == SPEC_PER_POSITION:
            position = _POSITION.search(match["labels"] or "")
            if position:
                key = position.group(1)
                per_position[key] = per_position.get(key, 0.0) + float(match["value"])
    return {
        "available": seen,
        "unix_seconds": time.time(),
        "counters": counters,
        "accepted_per_position": per_position,
    }


def spec_delta(before: dict | None, after: dict | None) -> dict | None:
    if not before or not after or not before.get("available") or not after.get("available"):
        return None
    counts = {
        name: after["counters"][name] - before["counters"][name]
        for name in SPEC_COUNTERS.values()
    }
    if any(value < 0 for value in counts.values()):
        return {"error": "spec-decode counters reset during measurement"}
    positions = {
        key: after["accepted_per_position"].get(key, 0.0) - before["accepted_per_position"].get(key, 0.0)
        for key in sorted(set(after["accepted_per_position"]) | set(before["accepted_per_position"]), key=int)
    }
    drafts, drafted, accepted = counts["drafts"], counts["draft_tokens"], counts["accepted_tokens"]
    return {
        **{key: int(value) for key, value in counts.items()},
        "acceptance_rate": accepted / drafted if drafted else None,
        "mean_acceptance_length": 1 + accepted / drafts if drafts else None,
        "per_position_acceptance_rate": {
            key: value / drafts for key, value in positions.items()
        } if drafts else {},
    }


# --------------------------------------------------------------------------
# Streaming


def stream(base_url: str, path: str, payload: dict, timeout: float = 3600) -> dict:
    """POST a streaming request and keep every token-bearing SSE chunk.

    Works for /v1/completions (``text``) and /v1/chat/completions
    (``delta.reasoning_content``/``delta.reasoning``/``delta.content``).
    Requires ``return_token_ids`` in the payload for token timing.
    """
    parsed = urlparse(server_root(base_url))
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False).encode()
    started_epoch = time.time()
    started = time.perf_counter()
    connection.request("POST", parsed.path.rstrip("/") + path, body=body,
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    if response.status != 200:
        error = response.read().decode("utf-8", errors="replace")
        connection.close()
        raise RuntimeError(f"HTTP {response.status}: {error}")
    chunks: list[dict] = []
    content: list[str] = []
    reasoning: list[str] = []
    usage = None
    finish_reason = None
    while True:
        raw_line = response.readline()
        if not raw_line:
            break
        observed = time.perf_counter() - started
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            break
        event = json.loads(data)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices") or ():
            delta = choice.get("delta") or {}
            text = choice.get("text") or delta.get("content") or ""
            thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
            ids = choice.get("token_ids") or []
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
            content.append(text)
            reasoning.append(thought)
            if ids or text or thought:
                chunks.append({"seconds": observed, "token_ids": ids,
                               "content_chars": len(text), "reasoning_chars": len(thought)})
    connection.close()
    return {
        "started_epoch_seconds": started_epoch,
        "started_perf_seconds": started,
        "request_seconds": time.perf_counter() - started,
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "usage": usage,
        "finish_reason": finish_reason,
        "chunks": chunks,
    }


def token_timing(result: dict) -> dict:
    """Client-observed timing over every streamed token ID.

    ``n_minus_one_tps`` is the recipe convention: (N-1) tokens over the
    first-to-last token window, excluding TTFT.  ``post_burst_tps`` also
    excludes every token in the first SSE chunk (a speculative burst).
    """
    token_chunks = [chunk for chunk in result["chunks"] if chunk["token_ids"]]
    count = sum(len(chunk["token_ids"]) for chunk in token_chunks)
    usage_tokens = (result.get("usage") or {}).get("completion_tokens")
    if not token_chunks:
        return {"completion_tokens_streamed": 0, "usage_completion_tokens": usage_tokens,
                "token_count_matches_usage": usage_tokens == 0, "ttft_seconds": None,
                "decode_seconds": None, "decode_tokens": 0, "n_minus_one_tps": None,
                "post_burst_tps": None}
    first, last = token_chunks[0]["seconds"], token_chunks[-1]["seconds"]
    seconds = last - first
    burst = len(token_chunks[0]["token_ids"])
    return {
        "completion_tokens_streamed": count,
        "usage_completion_tokens": usage_tokens,
        "token_count_matches_usage": usage_tokens == count,
        "ttft_seconds": first,
        "first_token_offset_seconds": first,
        "last_token_offset_seconds": last,
        "decode_seconds": seconds,
        "decode_tokens": count - 1,
        "n_minus_one_tps": (count - 1) / seconds if seconds > 0 else None,
        "post_burst_tps": (count - burst) / seconds if seconds > 0 else None,
    }


def completion_payload(model: str, prompt, max_tokens: int, *, temperature: float,
                       seed: int | None = None, force_length: bool = False, **extra) -> dict:
    """/v1/completions streaming payload. ``prompt`` may be text or token IDs."""
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        "return_token_ids": True,
    }
    if isinstance(prompt, list):
        payload["add_special_tokens"] = False
    if seed is not None:
        payload["seed"] = seed
    if force_length:
        payload.update({"min_tokens": max_tokens, "ignore_eos": True})
    payload.update(extra)
    return payload


def spread(values: list[float]) -> dict | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {"median": statistics.median(values), "min": min(values), "max": max(values),
            "n": len(values)}
