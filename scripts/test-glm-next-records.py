#!/usr/bin/env python3
"""Single-GPU numerical check: 656-byte GLM_NSA vs 528-byte GLM_NEXT MLA records.

Run inside the recipe image (candidate2 or later) on ONE idle GPU, e.g.:

    docker run --rm --gpus '"device=0"' --entrypoint python3 \\
        -v "$PWD/scripts:/scripts:ro" glm53-latestb12x:candidate2 \\
        /scripts/test-glm-next-records.py --heads 64

The same BF16 latents are written with both serving writers:

* 656 B ``fp8_ds_mla``: vLLM's ``concat_and_cache_mla`` (the op behind the
  backend's default ``do_kv_cache_update``) with an exact-zero 64-lane RoPE
  tail, exactly as the backend writes GLM-5.3 today.
* 528 B GLM_NEXT: B12x ``concat_and_cache_glm_next_mla`` through the prepared
  writer plan created by the backend helper.

Then the same queries and top-k selections run through prepared sparse-MLA
plans built by the backend's own ``_prepare_b12x_sparse_mla_plan`` helper:
GLM_NSA with a 576-wide query (512 + 64 zero lanes) and GLM_NEXT with the
512-wide query. Decode (split-K, natural-log LSE, forced maximal splits as in
serving), decode under CUDA-graph replay, and extend/prefill (base-2 LSE) are
compared. Both paths are also compared with an FP32 reference computed from
the dequantized 528-byte records.

Memory use is small (a few MiB of tensors plus kernel modules and the CUDA
context). The script exits non-zero if any tolerance is exceeded.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

import torch

LATENT = 512
ROPE_PAD = 64
PAGE = 64
FP8_RECORD = 656
NEXT_RECORD = 528
GROUP = 128


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--heads", type=int, default=64, help="64 owner, 32 TP2")
    parser.add_argument("--rows", type=int, default=8, help="decode query rows")
    parser.add_argument("--cache-tokens", type=int, default=4096)
    parser.add_argument("--width", type=int, default=2176, help="planned top-k width")
    parser.add_argument("--active", type=int, default=2048, help="max live top-k")
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--no-graph", action="store_true")
    parser.add_argument("--out-tol", type=float, default=3e-2)
    parser.add_argument("--cos-tol", type=float, default=0.9995)
    return parser.parse_args()


def _cdiv(a: int, b: int) -> int:
    return (a + b - 1) // b


def _dequant_next(records: torch.Tensor) -> torch.Tensor:
    """(T, 528) uint8 -> (T, 512) fp32 latent."""
    latent = records[:, :LATENT].contiguous().view(torch.float8_e4m3fn).float()
    scales = records[:, LATENT:NEXT_RECORD].contiguous().view(torch.float32)
    return (latent.view(-1, LATENT // GROUP, GROUP) * scales.unsqueeze(-1)).view(
        -1, LATENT
    )


def _reference(
    q: torch.Tensor,  # (R, H, 512) bf16
    latent: torch.Tensor,  # (T, 512) fp32 dequantized
    selected: torch.Tensor,  # (R, W) int32, -1 padded
    lengths: torch.Tensor,  # (R,) int32
    scale: float,
) -> tuple[torch.Tensor, torch.Tensor]:
    outs, lses = [], []
    for row in range(q.shape[0]):
        n = int(lengths[row])
        idx = selected[row, :n].long()
        keys = latent[idx]  # (n, 512)
        logits = q[row].float() @ keys.T * scale  # (H, n)
        lse = torch.logsumexp(logits, dim=-1)
        probs = torch.softmax(logits, dim=-1)
        outs.append(probs @ keys)
        lses.append(lse)
    return torch.stack(outs), torch.stack(lses)


def _compare(name: str, a: torch.Tensor, b: torch.Tensor) -> dict[str, float]:
    a32, b32 = a.float(), b.float()
    diff = (a32 - b32).abs()
    cos = torch.nn.functional.cosine_similarity(
        a32.flatten(), b32.flatten(), dim=0
    ).item()
    return {
        "check": name,
        "max_abs_diff": float(diff.max().item()),
        "mean_abs_diff": float(diff.mean().item()),
        "ref_max_abs": float(b32.abs().max().item()),
        "cosine": float(cos),
    }


def main() -> int:
    args = _parse()
    device = torch.device(args.device)
    torch.cuda.set_device(device)
    torch.manual_seed(args.seed)

    from vllm import _custom_ops as ops
    from b12x.attention import sparse_mla
    from vllm.v1.attention.backends.mla import b12x_mla_sparse as backend

    heads, rows, width = int(args.heads), int(args.rows), int(args.width)
    tokens = _cdiv(int(args.cache_tokens), PAGE) * PAGE
    pages = tokens // PAGE
    active = min(int(args.active), width, tokens)
    splits = _cdiv(width, 64)
    scale = 256**-0.5  # GLM-5.3 physical softmax scale (qk_head_dim=256)

    # Latents: realistic magnitudes, one all-zero group per 97th token to hit
    # the unit-scale path, and a few large outliers per group.
    kv_c = (torch.randn(tokens, LATENT, device=device) * 0.5).to(torch.bfloat16)
    kv_c[::97, :GROUP] = 0
    kv_c[::13, 5] = 6.0
    slots = torch.randperm(tokens, device=device).to(torch.int64)
    latent_by_slot = torch.empty_like(kv_c)
    latent_by_slot[slots] = kv_c

    # --- writers -------------------------------------------------------------
    cache_nsa = torch.zeros((pages, PAGE, FP8_RECORD), dtype=torch.uint8, device=device)
    k_pe = torch.zeros((tokens, ROPE_PAD), dtype=torch.bfloat16, device=device)
    ops.concat_and_cache_mla(
        kv_c,
        k_pe,
        cache_nsa,
        slots,
        kv_cache_dtype="fp8_ds_mla",
        scale=torch.ones((1,), dtype=torch.float32, device=device),
    )
    cache_next = torch.zeros((pages, PAGE, NEXT_RECORD), dtype=torch.uint8, device=device)
    writer_plan = backend._prepare_glm_next_writer_plan(device, PAGE)
    sparse_mla.concat_and_cache_glm_next_mla(kv_c, cache_next, slots, plan=writer_plan)
    torch.cuda.synchronize()

    nsa_flat = cache_nsa.view(tokens, FP8_RECORD)
    next_flat = cache_next.view(tokens, NEXT_RECORD)
    same_latent = (nsa_flat[:, :LATENT] == next_flat[:, :LATENT]).float().mean().item()
    nsa_scales = nsa_flat[:, LATENT:NEXT_RECORD].contiguous().view(torch.float32)
    next_scales = next_flat[:, LATENT:NEXT_RECORD].contiguous().view(torch.float32)
    rope_zero = bool((nsa_flat[:, NEXT_RECORD:] == 0).all().item())
    record_report = {
        "latent_bytes_identical_fraction": same_latent,
        "scale_max_rel_diff": float(
            ((nsa_scales - next_scales).abs() / nsa_scales.abs().clamp_min(1e-30))
            .max()
            .item()
        ),
        "nsa_rope_tail_all_zero": rope_zero,
    }
    deq_next = _dequant_next(next_flat)
    deq_err = (deq_next - latent_by_slot.float()).abs().max().item()
    record_report["next_dequant_max_abs_err_vs_bf16"] = float(deq_err)

    # --- queries / selections ---------------------------------------------------
    q = (torch.randn(rows, heads, LATENT, device=device) * 0.5).to(torch.bfloat16)
    q576 = torch.zeros((rows, heads, LATENT + ROPE_PAD), dtype=torch.bfloat16, device=device)
    q576[..., :LATENT].copy_(q)
    selected = torch.full((rows, width), -1, dtype=torch.int32, device=device)
    lengths = torch.empty((rows,), dtype=torch.int32, device=device)
    for row in range(rows):
        n = max(1, active - 97 * row) if row % 2 else active
        n = min(n, width)
        selected[row, :n] = torch.randperm(tokens, device=device)[:n].to(torch.int32)
        lengths[row] = n
    cache_lengths = torch.full((rows,), tokens, dtype=torch.int32, device=device)

    def make_caps(model: str, mode: str) -> object:
        next_mode = model == "next"
        return sparse_mla.Caps(
            device=device,
            num_q_heads=heads,
            max_q_rows=rows,
            max_width=width,
            softmax_scale=scale,
            dtype=torch.bfloat16,
            kv_dtype=torch.uint8,
            head_dim=LATENT if next_mode else LATENT + ROPE_PAD,
            v_head_dim=LATENT,
            model_type=int(
                sparse_mla.ModelType.GLM_NEXT if next_mode else sparse_mla.ModelType.GLM_NSA
            ),
            cache_record_bytes=NEXT_RECORD if next_mode else FP8_RECORD,
            mode=mode,
            max_batch=rows,
            max_chunks_per_row=splits,
            page_size=PAGE,
            head_major_output=True,
            return_lse=True,
            lse_scale="natural",
        )

    def run(model: str, mode: str, *, graph: bool = False):
        plan, _ = backend._prepare_b12x_sparse_mla_plan(
            sparse_mla, make_caps(model, mode), forced_num_splits=splits
        )
        scratch = torch.zeros(
            (backend._b12x_plan_scratch_nbytes(plan),), dtype=torch.uint8, device=device
        )
        binding = sparse_mla.bind(
            plan,
            scratch=scratch,
            q=(q if model == "next" else q576).contiguous(),
            kv_cache=cache_next if model == "next" else cache_nsa,
            selected_indices=selected,
            cache_lengths=cache_lengths,
            selected_lengths=lengths,
        )
        if not graph:
            out, lse = sparse_mla.run(binding)
            out, lse = out.clone(), lse.clone()
            if mode != "decode":
                lse = lse * math.log(2.0)  # prefill publishes base-2 LSE
            return out, lse
        sparse_mla.run(binding)  # warm on the capture stream's pool
        torch.cuda.synchronize()
        cuda_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(cuda_graph):
            out, lse = sparse_mla.run(binding)
        scratch.zero_()
        cuda_graph.replay()
        torch.cuda.synchronize()
        return out.clone(), lse.clone()

    ref_out, ref_lse = _reference(q, deq_next, selected, lengths, scale)
    results = []
    failures = []
    for mode in ("decode", "extend"):
        nsa_out, nsa_lse = run("nsa", mode)
        next_out, next_lse = run("next", mode)
        for check in (
            _compare(f"{mode}: out NEXT(528) vs NSA(656)", next_out, nsa_out),
            _compare(f"{mode}: lse NEXT(528) vs NSA(656)", next_lse, nsa_lse),
            _compare(f"{mode}: out NEXT vs fp32 reference", next_out, ref_out),
            _compare(f"{mode}: out NSA vs fp32 reference", nsa_out, ref_out),
            _compare(f"{mode}: lse NEXT vs fp32 reference", next_lse, ref_lse),
        ):
            results.append(check)
            tol = args.out_tol * max(1.0, check["ref_max_abs"])
            if check["cosine"] < args.cos_tol or check["max_abs_diff"] > tol:
                failures.append(check["check"])
    if not args.no_graph:
        for model in ("nsa", "next"):
            eager_out, eager_lse = run(model, "decode")
            graph_out, graph_lse = run(model, "decode", graph=True)
            same = bool(torch.equal(eager_out, graph_out) and torch.equal(eager_lse, graph_lse))
            results.append({"check": f"decode graph replay == eager ({model})", "equal": same})
            if not same:
                failures.append(f"decode graph replay ({model})")

    report = {
        "device": str(device),
        "gpu": torch.cuda.get_device_name(device),
        "heads": heads,
        "rows": rows,
        "width": width,
        "cache_tokens": tokens,
        "records": record_report,
        "results": results,
        "failures": failures,
    }
    print(json.dumps(report, indent=2))
    if record_report["latent_bytes_identical_fraction"] < 0.99:
        print("note: latent bytes differ beyond rounding between writers", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
