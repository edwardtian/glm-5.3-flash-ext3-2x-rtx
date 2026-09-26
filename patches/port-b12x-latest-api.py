#!/usr/bin/env python3
"""Adapt the patched GLM-5.3 vLLM tree to the current B12x preparation API.

B12x fork commit 7fcc094e merged upstream's plan-owned "preparation" lifecycle:
every planned family is declared with ``plan(...)``, prepared (materialized,
compiled and primed) once, and then bound and run per call.  The sparse-MLA
front doors ``run_decode``/``run_extend`` are gone, and numerical choices such
as the softmax scale, record format and LSE convention now live in the plan's
``Caps``.  This port runs after every other recipe patch and rewrites the
b12x call sites of the resulting tree.  It keeps the recipe's numerical
contract unchanged: 656-byte ``fp8_ds_mla`` GLM_NSA records, a 576-wide query
with 64 exact-zero RoPE lanes, forced maximal decode split-K and natural-log
LSEs for DCP.

Every edit is an exact, replace-once anchor.  The script is idempotent, fails
closed on source drift, and compiles every rewritten module before writing.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])


class Drift(RuntimeError):
    pass


def _replace_once(source: str, old: str, new: str, *, where: str) -> str:
    count = source.count(old)
    if count != 1:
        raise Drift(f"{where}: expected one anchor, found {count}: {old[:120]!r}")
    return source.replace(old, new)


def _replace_span(
    source: str, start: str, end: str, new: str, *, digest: str, where: str
) -> str:
    """Replace ``start ... end`` (inclusive) after checking the span's digest."""
    if source.count(start) != 1:
        raise Drift(f"{where}: span start drift: {start[:120]!r}")
    begin = source.index(start)
    stop = source.find(end, begin)
    if stop < 0:
        raise Drift(f"{where}: span end drift: {end[:120]!r}")
    stop += len(end)
    span = source[begin:stop]
    actual = hashlib.sha256(span.encode()).hexdigest()[:16]
    if actual != digest:
        raise Drift(
            f"{where}: span content drift (sha256[:16] {actual} != {digest}) "
            f"between {start[:60]!r} and {end[:60]!r}"
        )
    return source[:begin] + new + source[stop:]


def _patch(relative: str, marker: str, edit) -> None:
    path = ROOT / relative
    source = path.read_text()
    if marker in source:
        print(f"[skip] {relative}: already ported")
        return
    updated = edit(source)
    if marker not in updated:
        raise Drift(f"{relative}: port marker missing after rewrite")
    compile(updated, str(path), "exec")
    path.write_text(updated)
    print(f"[ok]   {relative}")


# ---------------------------------------------------------------------------
# 0. Shared preparation helper module (new file in the vLLM tree).
# ---------------------------------------------------------------------------

HELPER = "model_executor/layers/b12x_preparation.py"
HELPER_SOURCE = '''# SPDX-License-Identifier: Apache-2.0
"""Startup preparation helpers for the recipe's B12x call sites.

Current B12x declares every planned kernel family with ``plan(...)`` and
prepares (materializes, compiles and primes) the declaration once before it
is bound and run. ``prepare_default`` is the documented integration hook for a
caller without a startup preparation driver: it prepares one request with its
default configuration, compiles in-process, and refuses to run under CUDA
graph capture.

Every finished preparation also evicts compiled programs that no prepared
plan retains from B12x's kernel memos. The recipe still reaches several B12x
kernels through eager, memoized entry points (paged DSA indexer, row top-k,
GLM H64 query projection, NVFP4 cache writer). Eviction would force those to
recompile on their next use, which can be inside CUDA graph capture where a
CuTe compile or module load corrupts the capture. The pinned fork never
evicted, so the recipe restores that contract process-wide. Set
VLLM_B12X_EVICT_UNRETAINED=1 to keep B12x's eviction.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

_RETENTION_INSTALLED = False


def retain_b12x_programs() -> None:
    """Keep compiled B12x programs resident across preparation sessions."""
    global _RETENTION_INSTALLED
    if _RETENTION_INSTALLED:
        return
    _RETENTION_INSTALLED = True
    if os.getenv("VLLM_B12X_EVICT_UNRETAINED", "0") not in ("", "0", "false", "False"):
        return
    import b12x._lib.program_cache as program_cache

    if getattr(program_cache.evict_unretained, "_vllm_retains_programs", False):
        return

    def evict_unretained(keep: Any) -> int:
        del keep
        return 0

    evict_unretained._vllm_retains_programs = True  # type: ignore[attr-defined]
    program_cache.evict_unretained = evict_unretained


def _noop_prepare_call(state: Any) -> Any:
    from b12x.preparation import PreparedCall

    del state
    return PreparedCall(run=lambda: None)


def prepare_b12x_plan(
    plan: Any,
    *,
    name: str,
    prepare_call: Callable[[Any], Any] | None = None,
) -> Any:
    """Prepare ``plan`` now (never under capture) and return it.

    Families whose materialization already compiles and loads every launcher
    (PCIe collectives, GEMV, mHC) use the default no-op priming call, so no
    collective or kernel launch happens here.
    """
    retain_b12x_programs()
    if plan.prepared is not None:
        return plan
    from b12x.preparation import prepare_default

    prepare_default(
        plan.request(
            name=name,
            prepare_call=_noop_prepare_call if prepare_call is None else prepare_call,
        )
    )
    if plan.prepared is None:
        raise RuntimeError(f"B12x plan {name!r} was not prepared")
    return plan
'''


def _write_helper() -> None:
    path = ROOT / HELPER
    if path.exists():
        if path.read_text() != HELPER_SOURCE:
            raise Drift(f"{HELPER}: exists with different content")
        print(f"[skip] {HELPER}: already present")
        return
    compile(HELPER_SOURCE, str(path), "exec")
    path.write_text(HELPER_SOURCE)
    print(f"[ok]   {HELPER}")


# ---------------------------------------------------------------------------
# 1. Sparse MLA backend: run_decode/run_extend -> plan/prepare/bind/run.
# ---------------------------------------------------------------------------

SPARSE_MLA = "v1/attention/backends/mla/b12x_mla_sparse.py"
SPARSE_MLA_MARKER = "# b12x-latest-api: prepared sparse-MLA plans"

SPARSE_MLA_HELPERS_ANCHOR = """_KV_FP8_ROPE_REQUESTED = NVFP4_MLA_CACHE_FORMAT.fp8_rope
"""
SPARSE_MLA_HELPERS = SPARSE_MLA_HELPERS_ANCHOR + '''
# b12x-latest-api: prepared sparse-MLA plans
# Current B12x plans are declarations. Their prepared state fixes the decode
# row capacity and split count at the first prime, and preparing a plan for the
# first time under CUDA graph capture is an error. Plans are therefore shared by
# every layer with the same Caps and primed once here, eagerly, at their full
# planned capacity. The recipe's qualified decode forced the maximal split-K
# count (ceil(top-k / 64)); B12x now reads that choice once, at prime time, from
# B12X_MLA_SM120_NUM_SPLITS, so the prime pins it unless the operator set it.
_B12X_SPARSE_MLA_PLANS: dict[tuple[Any, int | None], Any] = {}
_B12X_MLA_NUM_SPLITS_ENV = "B12X_MLA_SM120_NUM_SPLITS"
_LN2 = 0.6931471805599453
_KV_FP8_ROPE_WRITER_PREWARMED: set[tuple[int | None, int, bool]] = set()


def _b12x_plan_scratch_nbytes(plan: Any) -> int:
    specs = tuple(plan.scratch_specs())
    if len(specs) != 1:
        raise RuntimeError(
            f"B12x sparse MLA expects one scratch buffer, got {len(specs)}"
        )
    return int(specs[0].nbytes)


def _prepare_b12x_sparse_mla_plan(
    sparse_mla: Any,
    caps: Any,
    *,
    forced_num_splits: int | None,
) -> tuple[Any, bool]:
    """Return a prepared shared plan and whether this call prepared it."""
    key = (caps, forced_num_splits if caps.mode == "decode" else None)
    plan = _B12X_SPARSE_MLA_PLANS.get(key)
    if plan is not None:
        return plan, False
    plan = sparse_mla.plan(caps)
    if caps.device.type != "cuda":
        _B12X_SPARSE_MLA_PLANS[key] = plan
        return plan, False
    from b12x.preparation import PreparedCall

    from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

    def prepare_call(state: Any) -> Any:
        device = caps.device
        rows = int(caps.max_q_rows)
        spec = state.scratch_specs()[0]
        scratch = torch.zeros(spec.shape, dtype=spec.dtype, device=device)
        q = torch.zeros(
            (rows, int(caps.num_q_heads), int(caps.head_dim)),
            dtype=caps.dtype,
            device=device,
        )
        # One zero page; every selected slot points at slot zero.
        kv_cache = torch.zeros(
            (1, int(caps.page_size), int(caps.cache_record_bytes)),
            dtype=caps.kv_dtype,
            device=device,
        )
        selected = torch.zeros(
            (rows, int(caps.max_width)), dtype=torch.int32, device=device
        )
        cache_lengths = torch.full(
            (int(caps.max_batch),),
            int(caps.page_size),
            dtype=torch.int32,
            device=device,
        )
        selected_lengths = torch.ones((rows,), dtype=torch.int32, device=device)
        runtime = state.bind(
            scratch=scratch,
            q=q,
            kv_cache=kv_cache,
            selected_indices=selected,
            cache_seqlens_int32=cache_lengths,
            nsa_cache_seqlens_int32=selected_lengths,
        )
        pin_splits = (
            caps.mode == "decode"
            and forced_num_splits is not None
            and not os.environ.get(_B12X_MLA_NUM_SPLITS_ENV)
        )
        if pin_splits:
            os.environ[_B12X_MLA_NUM_SPLITS_ENV] = str(int(forced_num_splits))
        try:
            state.prime(runtime, kv_cache=kv_cache)
        finally:
            if pin_splits:
                os.environ.pop(_B12X_MLA_NUM_SPLITS_ENV, None)
        return PreparedCall(run=lambda: state.run(runtime, kv_cache=kv_cache))

    name = (
        f"vllm.b12x_mla_sparse.{caps.mode}.h{int(caps.num_q_heads)}"
        f".rows{int(caps.max_q_rows)}.w{int(caps.max_width)}"
        f".rec{int(caps.cache_record_bytes)}"
    )
    prepare_b12x_plan(plan, name=name, prepare_call=prepare_call)
    prepared = getattr(plan, "prepared", None)
    launch = getattr(getattr(prepared, "state", None), "prepared", None)
    if launch is None:
        raise RuntimeError(f"B12x sparse MLA plan {name} was not primed")
    if caps.mode == "decode":
        logger.info_once(
            "B12x sparse MLA decode plan %s: rows<=%d splits=%d/%d",
            name,
            int(launch.rows),
            int(launch.num_splits),
            int(launch.num_chunks),
        )
    _B12X_SPARSE_MLA_PLANS[key] = plan
    return plan, True
'''

SPARSE_MLA_PLAN_START = """        # Lazily import SparkInfer only on this opt-in path.
        from b12x.attention.sparse_mla import (
            Caps as B12XSparseMLAScratchCaps,
        )
"""
SPARSE_MLA_PLAN_END = """        self._scratch_nbytes = max(
            int(self._decode_plan.layout.nbytes),
            int(self._extend_plan.layout.nbytes),
        )
"""
SPARSE_MLA_PLAN_DIGEST = "7e0fb62a14155543"
SPARSE_MLA_PLAN_NEW = """        # Lazily import SparkInfer only on this opt-in path. The current B12x
        # API declares one immutable plan per mode; its Caps own the softmax
        # scale, cache record, split capacity and LSE convention, and bind/run
        # consume only live tensors. Plans are shared across layers and
        # prepared (compiled and primed at full capacity) before any capture.
        from b12x.attention import sparse_mla as b12x_sparse_mla

        self._b12x_sparse_mla = b12x_sparse_mla
        self._b12x_prepared_new_plan = False
        glm_nsa = int(b12x_sparse_mla.ModelType.GLM_NSA)
        nvfp4_record = self._b12x_scale_format is not None

        # Eager PLAN -> PREPARE -> BIND -> RUN (no b12x workspace/arena, ever).
        # Each forward maps a vLLM workspace-manager scratch tensor into the
        # plan's views via sparse_mla.bind(). Decode returns the natural-log
        # LSE required by the DCP merge directly; the extend plan always owns
        # its final-LSE view (so prefill never allocates it) and publishes the
        # kernel's base-2 LSE, which forward_mqa converts for DCP.
        def _make_plan(
            mode: str, max_q_rows: int, num_q_heads: int, max_batch: int
        ) -> Any:
            caps = b12x_sparse_mla.Caps(
                device=self.device,
                num_q_heads=int(num_q_heads),
                max_q_rows=int(max_q_rows),
                max_width=self.topk_tokens,
                softmax_scale=self.scale,
                dtype=torch.bfloat16,
                kv_dtype=torch.uint8,
                head_dim=self.q_head_dim,
                v_head_dim=self.kv_lora_rank,
                model_type=glm_nsa,
                scale_format=self._b12x_scale_format,
                cache_record_bytes=self._kv_record_bytes,
                fp8_rope=bool(self._kv_fp8_rope) if nvfp4_record else None,
                latent_scale_per_token=bool(self._nvfp4_dynamic_scale),
                mode=mode,
                max_batch=int(max_batch),
                max_chunks_per_row=self._num_splits_cap,
                page_size=self.block_size,
                head_major_output=self._head_major_mla_output,
                return_lse=(
                    bool(self.need_to_return_lse_for_decode)
                    if mode == "decode"
                    else True
                ),
                lse_scale="natural",
            )
            plan, prepared_now = _prepare_b12x_sparse_mla_plan(
                b12x_sparse_mla,
                caps,
                forced_num_splits=self._num_splits_cap,
            )
            self._b12x_prepared_new_plan |= prepared_now
            return plan

        self._decode_plan = _make_plan(
            "decode",
            self._decode_max_rows,
            self._kernel_num_heads,
            self._decode_max_rows,
        )
        self._extend_plan = _make_plan(
            "extend", max_batched, self._kernel_num_heads, max_num_seqs
        )
        # One caller-owned uint8 scratch tensor covers either path (the larger
        # layout); the per-mode materializer carves its views from the prefix.
        self._scratch_nbytes = max(
            _b12x_plan_scratch_nbytes(self._decode_plan),
            _b12x_plan_scratch_nbytes(self._extend_plan),
        )
"""

SPARSE_MLA_CKV_OLD = """                int(self._ckv_extend_plan.layout.nbytes),
"""
SPARSE_MLA_CKV_NEW = """                _b12x_plan_scratch_nbytes(self._ckv_extend_plan),
"""

SPARSE_MLA_PREWARM_START = """    def _prewarm_extend_kernels_once(self, max_batched: int) -> None:
"""
SPARSE_MLA_PREWARM_END = """                    **kernel_format_kwargs,
                )
            self._sync_warmup()
"""
SPARSE_MLA_PREWARM_DIGEST = "e92e6e90bc3ce5d7"
SPARSE_MLA_PREWARM_NEW = """    def _prewarm_extend_kernels_once(self, max_batched: int) -> None:
        # Plans are compiled and primed at full capacity by _make_plan. Keep
        # the DCP ranks in step after a rank prepared new shared plans, as the
        # former extend prewarm did.
        del max_batched
        self._prewarm_kv_fp8_rope_writer_once()
        if self._b12x_prepared_new_plan:
            self._sync_warmup()

    def _prewarm_kv_fp8_rope_writer_once(self) -> None:
        # Current B12x refuses to compile the 368-byte NVFP4 writer under CUDA
        # graph capture (the pinned fork compiled it silently there). The
        # profile run sees an empty cache, so compile the exact writer
        # specialization now; slot -1 is skipped and writes nothing.
        if not self._kv_fp8_rope or self.device.type != "cuda":
            return
        key = (
            self.device.index,
            int(self.block_size),
            bool(self._nvfp4_dynamic_scale),
        )
        if key in _KV_FP8_ROPE_WRITER_PREWARMED:
            return
        kv_c = torch.zeros(
            (1, self.kv_lora_rank), dtype=torch.bfloat16, device=self.device
        )
        k_pe = torch.zeros((1, 64), dtype=torch.bfloat16, device=self.device)
        cache = torch.zeros(
            (1, self.block_size, self._kv_record_bytes),
            dtype=torch.uint8,
            device=self.device,
        )
        slots = torch.full((1,), -1, dtype=torch.int64, device=self.device)
        if self._nvfp4_dynamic_scale:
            self._concat_and_cache_nvfp4_mla_fp8_rope(
                kv_c, k_pe, cache, slots, None, per_token_scale=True
            )
        else:
            self._concat_and_cache_nvfp4_mla_fp8_rope(
                kv_c, k_pe, cache, slots, None
            )
        torch.accelerator.synchronize(self.device)
        _KV_FP8_ROPE_WRITER_PREWARMED.add(key)
"""

SPARSE_MLA_LATENT_OLD = """        kernel_format_kwargs = self._b12x_kernel_format_kwargs(latent_scale)
"""
SPARSE_MLA_LATENT_NEW = """        # Validates the dynamic-scale contract; the record format itself is
        # fixed by the prepared plans.
        self._b12x_kernel_format_kwargs(latent_scale)
        if float(latent_scale) != 1.0:
            raise RuntimeError(
                "B12X_MLA_SPARSE: the current B12x sparse-MLA plan API does not "
                "apply a per-layer NVFP4 outer latent scale (got "
                f"{latent_scale!r}); unset VLLM_NVFP4_MLA_SCALES_FILE or use "
                "VLLM_NVFP4_MLA_DYNAMIC_SCALE=1 / fp8_ds_mla"
            )
"""

SPARSE_MLA_DECODE_START = """            binding = self._decode_plan.bind(
                scratch=scratch_storage,
                q=decode_q,
"""
SPARSE_MLA_DECODE_END = """                    forced_num_splits=self._num_splits_cap,
                    **kernel_format_kwargs,
                ),
            )
"""
SPARSE_MLA_DECODE_DIGEST = "02337b4d6ab8df4d"
SPARSE_MLA_DECODE_NEW = """            binding = self._b12x_sparse_mla.bind(
                self._decode_plan,
                scratch=scratch_storage,
                q=decode_q,
                kv_cache=kv_cache,
                selected_indices=selected_indices,
                cache_lengths=cache_seqlens,
                selected_lengths=nsa_cache_seqlens,
            )
            if self.need_to_return_lse_for_decode:
                # The decode plan returns (O, natural-log LSE).
                out, lse = cast(
                    tuple[torch.Tensor, torch.Tensor],
                    self._b12x_sparse_mla.run(binding),
                )
                if self._pad_heads:
                    assert dense_out_workspace is not None
                    dense_out = dense_out_workspace[:num_actual_toks]
                    dense_out.copy_(out[:, : self._input_num_heads, :])
                    out = dense_out
                    lse = lse[:, : self._input_num_heads]
                return out, lse
            out = cast(torch.Tensor, self._b12x_sparse_mla.run(binding))
"""

SPARSE_MLA_EXTEND_START = """            binding = extend_plan.bind(
                scratch=scratch_storage,
                q=prefill_q,
"""
SPARSE_MLA_EXTEND_END = """                        v_head_dim=self.kv_lora_rank,
                        **kernel_format_kwargs,
                    ),
                )
"""
SPARSE_MLA_EXTEND_DIGEST = "3b4474b9b48232f9"
SPARSE_MLA_EXTEND_NEW = """            binding = self._b12x_sparse_mla.bind(
                extend_plan,
                scratch=scratch_storage,
                q=prefill_q,
                kv_cache=kv_cache,
                selected_indices=selected_indices,
                cache_lengths=cache_seqlens,
                selected_lengths=nsa_cache_seqlens,
            )
            # B12x prefill returns (O, base-2 LSE) in the plan's scratch views.
            out, lse_base2 = cast(
                tuple[torch.Tensor, torch.Tensor],
                self._b12x_sparse_mla.run(binding),
            )
            lse = None
            if self.need_to_return_lse_for_decode and not use_ckv_gather:
                # DCP merges natural-log LSEs (the former lse_scale="natural").
                lse = lse_base2 * _LN2
"""


SPARSE_MLA_DOC_OLD = """backend via the ``b12x.attention.sparse_mla`` front door (``run_decode`` /
``run_extend``). On SM120+ CUDA those front-door functions route to SparkInfer's
unified MLA implementation automatically (GLM_NSA q_head_dim==576 contract).
"""
SPARSE_MLA_DOC_NEW = """backend via the ``b12x.attention.sparse_mla`` plan API (``plan`` ->
prepare -> ``bind`` -> ``run``), which routes to SparkInfer's unified MLA
implementation (explicit GLM_NSA q_head_dim==576 contract).
"""


def _port_sparse_mla(source: str) -> str:
    where = SPARSE_MLA
    source = _replace_once(source, SPARSE_MLA_DOC_OLD, SPARSE_MLA_DOC_NEW, where=where)
    source = _replace_once(
        source, SPARSE_MLA_HELPERS_ANCHOR, SPARSE_MLA_HELPERS, where=where
    )
    source = _replace_span(
        source,
        SPARSE_MLA_PLAN_START,
        SPARSE_MLA_PLAN_END,
        SPARSE_MLA_PLAN_NEW,
        digest=SPARSE_MLA_PLAN_DIGEST,
        where=where,
    )
    source = _replace_once(source, SPARSE_MLA_CKV_OLD, SPARSE_MLA_CKV_NEW, where=where)
    source = _replace_span(
        source,
        SPARSE_MLA_PREWARM_START,
        SPARSE_MLA_PREWARM_END,
        SPARSE_MLA_PREWARM_NEW,
        digest=SPARSE_MLA_PREWARM_DIGEST,
        where=where,
    )
    source = _replace_once(
        source, SPARSE_MLA_LATENT_OLD, SPARSE_MLA_LATENT_NEW, where=where
    )
    source = _replace_span(
        source,
        SPARSE_MLA_DECODE_START,
        SPARSE_MLA_DECODE_END,
        SPARSE_MLA_DECODE_NEW,
        digest=SPARSE_MLA_DECODE_DIGEST,
        where=where,
    )
    source = _replace_span(
        source,
        SPARSE_MLA_EXTEND_START,
        SPARSE_MLA_EXTEND_END,
        SPARSE_MLA_EXTEND_NEW,
        digest=SPARSE_MLA_EXTEND_DIGEST,
        where=where,
    )
    for removed in (
        "run_decode",
        "run_extend",
        "self._sparse_mla_decode_forward(",
        "self._sparse_mla_extend_forward(",
        ".layout.nbytes",
    ):
        if removed in source:
            raise Drift(f"{where}: stale B12x API reference remains: {removed}")
    return source


# ---------------------------------------------------------------------------
# 2. DSA indexer: the public dsa_indexer Caps/plan are now preparation
#    declarations and index_topk_fp8 / SOURCE_LAYOUT_PAGED are no longer
#    exported. The eager scratch planner and paged entry point that the
#    prepared path wraps are unchanged, so bind them directly. The DCP owner
#    exchange now needs an exact-row comm.pcie plan per call.
# ---------------------------------------------------------------------------

INDEXER = "model_executor/layers/sparse_attn_indexer.py"
INDEXER_MARKER = "# b12x-latest-api: eager paged DSA indexer"

INDEXER_RUN_OLD = """    from b12x.attention.dsa_indexer import (
        PAGED_INDEX_PAGE_SIZE,
        index_topk_fp8,
    )
    from b12x.attention.dsa_indexer import (
        SOURCE_LAYOUT_PAGED as INDEXER_SOURCE_LAYOUT_PAGED,
    )
    from b12x.attention.dsa_indexer import (
        Caps as B12XIndexerScratchCaps,
    )
    from b12x.attention.dsa_indexer import (
        plan as plan_indexer_scratch,
    )
"""
INDEXER_RUN_NEW = """    # b12x-latest-api: eager paged DSA indexer. Current B12x exposes
    # dsa_indexer.{Caps,plan} as preparation declarations and no longer
    # exports index_topk_fp8/SOURCE_LAYOUT_PAGED; the eager scratch planner
    # and paged entry point (which the prepared path itself wraps) remain.
    from b12x.attention.dsa_indexer.paged import (
        PAGED_INDEX_PAGE_SIZE,
        index_topk_fp8,
    )
    from b12x.attention.dsa_indexer.scratch import (
        INDEXER_SOURCE_LAYOUT_PAGED,
        B12XIndexerScratchCaps,
        plan_indexer_scratch,
    )
"""
INDEXER_RESERVE_OLD = """    from b12x.attention.dsa_indexer import (
        PAGED_INDEX_PAGE_SIZE,
    )
    from b12x.attention.dsa_indexer import (
        SOURCE_LAYOUT_PAGED as INDEXER_SOURCE_LAYOUT_PAGED,
    )
    from b12x.attention.dsa_indexer import (
        Caps as B12XIndexerScratchCaps,
    )
    from b12x.attention.dsa_indexer import (
        plan as plan_indexer_scratch,
    )
"""
INDEXER_RESERVE_NEW = """    from b12x.attention.dsa_indexer.paged import PAGED_INDEX_PAGE_SIZE
    from b12x.attention.dsa_indexer.scratch import (
        INDEXER_SOURCE_LAYOUT_PAGED,
        B12XIndexerScratchCaps,
        plan_indexer_scratch,
    )
"""
INDEXER_OWNER_ANCHOR = """def _b12x_dcp_topk_owner_min_rows(topk_tokens: int) -> int:
"""
INDEXER_OWNER_NEW = (
    '''_B12X_DCP_TOPK_OWNER_PLANS: dict[tuple[int, int], object] = {}


def _b12x_dcp_topk_owner_stage(
    owner_exchange: object,
    local_indices: torch.Tensor,
    local_scores: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Stage candidates through an exact-row prepared comm.pcie plan.

    Current B12x requires ``stage_candidates(..., plan=...)`` whose declared
    row count equals the live row count. The launcher depends only on the
    exchange's (world, rank, top-k, threads) and materialization compiles and
    loads it without a collective, so a new prefill row count costs one
    host-side declaration. The owner path is prefill-only (rows >= its
    qualified minimum) and never runs under CUDA graph capture.
    """
    key = (id(owner_exchange), int(local_indices.shape[0]))
    plan = _B12X_DCP_TOPK_OWNER_PLANS.get(key)
    if plan is None:
        from b12x.comm.pcie import plan as plan_pcie
        from b12x.comm.pcie import query_from_runtime

        from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

        plan = plan_pcie(
            query_from_runtime(
                owner_exchange,
                surface="DcpTopKOwnerExchange.stage_candidates",
                call={"local_indices": local_indices, "local_scores": local_scores},
            ),
            runtime=owner_exchange,
        )
        prepare_b12x_plan(plan, name=f"vllm.b12x_dcp_topk_owner.rows{key[1]}")
        _B12X_DCP_TOPK_OWNER_PLANS[key] = plan
    return owner_exchange.stage_candidates(local_indices, local_scores, plan=plan)


'''
    + INDEXER_OWNER_ANCHOR
)
INDEXER_STAGE_A_OLD = """    candidate_indices, candidate_scores = owner_exchange.stage_candidates(
        topk_indices,
        local_scores,
    )
"""
INDEXER_STAGE_A_NEW = """    candidate_indices, candidate_scores = _b12x_dcp_topk_owner_stage(
        owner_exchange,
        topk_indices,
        local_scores,
    )
"""
INDEXER_STAGE_B_OLD = """        candidate_indices, candidate_scores = owner_exchange.stage_candidates(
            topk_indices,
            topk_scores,
        )
"""
INDEXER_STAGE_B_NEW = """        candidate_indices, candidate_scores = _b12x_dcp_topk_owner_stage(
            owner_exchange,
            topk_indices,
            topk_scores,
        )
"""


def _port_indexer(source: str) -> str:
    where = INDEXER
    for old, new in (
        (INDEXER_RUN_OLD, INDEXER_RUN_NEW),
        (INDEXER_RESERVE_OLD, INDEXER_RESERVE_NEW),
        (INDEXER_OWNER_ANCHOR, INDEXER_OWNER_NEW),
        (INDEXER_STAGE_A_OLD, INDEXER_STAGE_A_NEW),
        (INDEXER_STAGE_B_OLD, INDEXER_STAGE_B_NEW),
    ):
        source = _replace_once(source, old, new, where=where)
    for removed in (
        "SOURCE_LAYOUT_PAGED as INDEXER_SOURCE_LAYOUT_PAGED",
        "Caps as B12XIndexerScratchCaps",
        "owner_exchange.stage_candidates(\n",
    ):
        if removed in source:
            raise Drift(f"{where}: stale B12x API reference remains: {removed!r}")
    return source


# ---------------------------------------------------------------------------
# 3. PCIe collectives: every pool method now takes a prepared ``plan=``.
#    The installed adapters (vendored sources plus port-b12x-dcp-a2a) are
#    rewritten in place, so the vendored files still build the pinned fork.
# ---------------------------------------------------------------------------

ALL_REDUCE = "distributed/device_communicators/b12x_pcie_all_reduce.py"
ALL_REDUCE_MARKER = "# b12x-latest-api: prepared oneshot plans"

ALL_REDUCE_IMPORT_OLD = """from b12x.comm.pcie import OneshotAllReducePool
"""
ALL_REDUCE_IMPORT_NEW = """from b12x.comm.pcie import OneshotAllReducePool
from vllm.model_executor.layers.b12x_preparation import (
    prepare_b12x_plan,
    retain_b12x_programs,
)
"""
ALL_REDUCE_INIT_OLD = """        self.runtime.for_stream()
        self.disabled = False
"""
ALL_REDUCE_INIT_NEW = """        self.runtime.for_stream()
        # b12x-latest-api: prepared oneshot plans. Current B12x runs each
        # all-reduce from a plan prepared for the exact (dtype, shape, stride)
        # against the channel; preparation installs the graph launch metadata
        # (including the TP2 generation-safe peer-push slots) and must finish
        # before CUDA graph capture.
        retain_b12x_programs()
        self._plans: dict[tuple[object, ...], object] = {}
        self.disabled = False
"""
ALL_REDUCE_SHOULD_OLD = """        return self.runtime.for_stream().should_allreduce(inp)

    def custom_all_reduce(self, inp: torch.Tensor) -> torch.Tensor | None:
        if not self.should_custom_ar(inp):
            return None
        return self.runtime.all_reduce(inp)
"""
ALL_REDUCE_SHOULD_NEW = """        if not self.runtime.for_stream().should_allreduce(inp):
            return False
        # A shape first seen inside torch capture can no longer be prepared.
        # Every TP rank captures the same shapes after the same eager warmups,
        # so this PyNCCL fallback is rank-symmetric.
        if torch.cuda.is_current_stream_capturing():
            return self._plan_key(inp) in self._plans
        return True

    @staticmethod
    def _plan_key(inp: torch.Tensor) -> tuple[object, ...]:
        return (inp.dtype, tuple(inp.shape), tuple(inp.stride()), inp.device.index)

    def _plan_for(self, inp: torch.Tensor) -> object:
        key = self._plan_key(inp)
        plan = self._plans.get(key)
        if plan is not None:
            return plan
        from b12x.comm.pcie import plan as plan_pcie
        from b12x.comm.pcie import query_from_runtime
        from b12x.preparation import PreparedCall

        channel = self.runtime.for_stream()
        plan = plan_pcie(
            query_from_runtime(
                channel,
                surface="OneshotAllReducePool.all_reduce",
                call={"inp": inp},
            ),
            runtime=channel,
        )

        def prepare_call(state: object) -> PreparedCall:
            # Install the exact graph plan for this contract. Materialization
            # already compiled and loaded the launchers; no collective runs.
            state.bind_input(inp)
            return PreparedCall(run=lambda: None)

        prepare_b12x_plan(
            plan,
            name=(
                f"vllm.b12x_pcie.all_reduce.{inp.dtype}.{tuple(inp.shape)}"
                f".{tuple(inp.stride())}"
            ),
            prepare_call=prepare_call,
        )
        self._plans[key] = plan
        return plan

    def custom_all_reduce(self, inp: torch.Tensor) -> torch.Tensor | None:
        if not self.should_custom_ar(inp):
            return None
        return self.runtime.all_reduce(inp, plan=self._plan_for(inp))
"""
ALL_REDUCE_CLOSE_OLD = """        self.runtime.close()
        self._closed = True
        self.disabled = True
"""
ALL_REDUCE_CLOSE_NEW = """        self.runtime.close()
        self._closed = True
        self.disabled = True
        self._plans.clear()
"""


def _port_all_reduce(source: str) -> str:
    for old, new in (
        (ALL_REDUCE_IMPORT_OLD, ALL_REDUCE_IMPORT_NEW),
        (ALL_REDUCE_INIT_OLD, ALL_REDUCE_INIT_NEW),
        (ALL_REDUCE_SHOULD_OLD, ALL_REDUCE_SHOULD_NEW),
        (ALL_REDUCE_CLOSE_OLD, ALL_REDUCE_CLOSE_NEW),
    ):
        source = _replace_once(source, old, new, where=ALL_REDUCE)
    return source


DCP_A2A = "distributed/device_communicators/b12x_dcp_a2a.py"
DCP_A2A_MARKER = "# b12x-latest-api: prepared DCP gather/combine plans"

DCP_A2A_PREPARE_OLD = """        # CuTe compilation is not legal once torch's graph capture has begun.
        self.pool.prepare_graph_all_gather_heads()
        self.pool.prepare_graph_lse_reduce_scatter(dtype=torch.bfloat16)
"""
DCP_A2A_PREPARE_NEW = """        # CuTe compilation is not legal once torch's graph capture has begun.
        # b12x-latest-api: prepared DCP gather/combine plans. The removed
        # prepare_graph_* hooks are replaced by comm.pcie plans prepared now,
        # against the pool's channel. Their launchers depend only on (world,
        # rank, threads) and, for the combine, the BF16 output dtype; the call
        # shapes below are declaration metadata and are not rechecked per call.
        from b12x.comm.pcie import plan as plan_pcie
        from b12x.comm.pcie import query_from_runtime

        from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

        channel = self.pool.for_stream()
        heads_per_rank = self.total_heads // int(self.pool.world_size)
        gather_input = torch.zeros(
            (1, heads_per_rank, self.query_head_dim),
            dtype=torch.bfloat16,
            device=self.device,
        )
        combine_output = torch.zeros(
            (1, self.total_heads, self.output_head_dim),
            dtype=torch.bfloat16,
            device=self.device,
        )
        combine_lse = torch.zeros(
            (1, self.total_heads), dtype=torch.float32, device=self.device
        )
        self.gather_plan = prepare_b12x_plan(
            plan_pcie(
                query_from_runtime(
                    channel,
                    surface="DcpAllToAllPool.all_gather_heads",
                    call={"local_input": gather_input},
                ),
                runtime=channel,
            ),
            name="vllm.b12x_dcp_a2a.all_gather_heads",
        )
        self.combine_plan = prepare_b12x_plan(
            plan_pcie(
                query_from_runtime(
                    channel,
                    surface="DcpAllToAllPool.lse_reduce_scatter",
                    call={
                        "partial_output": combine_output,
                        "partial_lse": combine_lse,
                        "is_lse_base_on_e": True,
                    },
                ),
                runtime=channel,
            ),
            name="vllm.b12x_dcp_a2a.lse_reduce_scatter.bf16",
        )
"""
DCP_A2A_DTYPE_OLD = """            and partial_output.dtype in (torch.bfloat16, torch.float16)
"""
DCP_A2A_DTYPE_NEW = """            # The prepared combine launcher is specialized for BF16.
            and partial_output.dtype == torch.bfloat16
"""
DCP_A2A_GATHER_OLD = """        return self.pool.all_gather_heads(query)
"""
DCP_A2A_GATHER_NEW = """        return self.pool.all_gather_heads(query, plan=self.gather_plan)
"""
DCP_A2A_COMBINE_OLD = """        return self.pool.lse_reduce_scatter(
            partial_output,
            partial_lse,
            is_lse_base_on_e=is_lse_base_on_e,
        )
"""
DCP_A2A_COMBINE_NEW = """        return self.pool.lse_reduce_scatter(
            partial_output,
            partial_lse,
            plan=self.combine_plan,
            is_lse_base_on_e=is_lse_base_on_e,
        )
"""


def _port_dcp_a2a(source: str) -> str:
    for old, new in (
        (DCP_A2A_PREPARE_OLD, DCP_A2A_PREPARE_NEW),
        (DCP_A2A_DTYPE_OLD, DCP_A2A_DTYPE_NEW),
        (DCP_A2A_GATHER_OLD, DCP_A2A_GATHER_NEW),
        (DCP_A2A_COMBINE_OLD, DCP_A2A_COMBINE_NEW),
    ):
        source = _replace_once(source, old, new, where=DCP_A2A)
    if "self.pool.prepare_graph_" in source:
        raise Drift(f"{DCP_A2A}: removed prepare_graph_* hook remains")
    return source


# ---------------------------------------------------------------------------
# 4. mHC: run_post_pre now requires a prepared functional plan.
# ---------------------------------------------------------------------------

MHC = "model_executor/layers/mhc.py"
MHC_MARKER = "# b12x-latest-api: prepared mHC post+pre plans"

MHC_HELPER_ANCHOR = """logger = init_logger(__name__)
"""
MHC_HELPER_NEW = (
    MHC_HELPER_ANCHOR
    + '''
# b12x-latest-api: prepared mHC post+pre plans
# Current B12x runs mHC only from a prepared plan whose invocation fixes the
# numerical recipe. Custom ops carry the plan handle, which resolves through a
# weak registry, so the plan is held here for the process lifetime.
_B12X_MHC_POST_PRE_PLANS: dict[tuple[object, ...], object] = {}


def _b12x_mhc_post_pre_plan(
    residual: torch.Tensor,
    norm_weight: torch.Tensor,
    *,
    rms_eps: float,
    hc_eps: float,
    sinkhorn_iters: int,
    norm_eps: float,
) -> object | None:
    key = (
        residual.device,
        int(residual.shape[-1]),
        norm_weight.dtype,
        float(rms_eps),
        float(hc_eps),
        int(sinkhorn_iters),
        float(norm_eps),
    )
    plan = _B12X_MHC_POST_PRE_PLANS.get(key)
    if plan is not None:
        return plan
    if torch.cuda.is_current_stream_capturing():
        # Preparation is illegal under capture; keep TileLang for this graph.
        return None
    from b12x.norm import mhc as b12x_mhc

    from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

    plan = b12x_mhc.plan(
        b12x_mhc.Caps(
            device=residual.device,
            max_tokens=1,
            hidden_size=int(residual.shape[-1]),
        ),
        invocation={
            "operation": "post_pre",
            "output_mode": "functional",
            "has_norm_weight": True,
            "norm_weight_dtype": str(norm_weight.dtype).removeprefix("torch."),
            "has_fn_bf16": False,
            "rms_eps": float(rms_eps),
            "hc_eps": float(hc_eps),
            "sinkhorn_iters": int(sinkhorn_iters),
            "norm_eps": float(norm_eps),
        },
    )
    prepare_b12x_plan(plan, name="vllm.mhc.glm_h4096.post_pre.m1")
    _B12X_MHC_POST_PRE_PLANS[key] = plan
    return plan
'''
)
MHC_USE_OLD = """        if use_b12x:
            from b12x.norm import mhc as b12x_mhc
"""
MHC_USE_NEW = """        b12x_mhc_plan = (
            _b12x_mhc_post_pre_plan(
                residual,
                norm_weight,
                rms_eps=rms_eps,
                hc_eps=hc_pre_eps,
                sinkhorn_iters=sinkhorn_repeat,
                norm_eps=norm_eps,
            )
            if use_b12x
            else None
        )
        if b12x_mhc_plan is not None:
            from b12x.norm import mhc as b12x_mhc
"""
MHC_CALL_OLD = """                norm_weight=norm_weight,
                norm_eps=norm_eps,
            )
            # Preserve vLLM's public post-mix ABI"""
MHC_CALL_NEW = """                norm_weight=norm_weight,
                norm_eps=norm_eps,
                plan=b12x_mhc_plan,
            )
            # Preserve vLLM's public post-mix ABI"""


def _port_mhc(source: str) -> str:
    for old, new in (
        (MHC_HELPER_ANCHOR, MHC_HELPER_NEW),
        (MHC_USE_OLD, MHC_USE_NEW),
        (MHC_CALL_OLD, MHC_CALL_NEW),
    ):
        source = _replace_once(source, old, new, where=MHC)
    return source


# ---------------------------------------------------------------------------
# 5. EXL3: fused MoE / EP / BF16 GEMV.
#    B12x removed its vLLM compatibility shim (quant_modes planning,
#    Caps/plan/required_nbytes) together with the old public fused_moe
#    surface. Projection-mixed tiers, EP route maps, BF16 epilogues and K2
#    uniform layers are not expressible through plan_execution, so bind the
#    same scratch-planned _impl layer that the removed shim wrapped. The
#    prepared ep_moe path no longer compiles full-rotation Trellis, so uniform
#    EP layers use the fused route-map path already used by projection EP.
# ---------------------------------------------------------------------------

EXL3 = "model_executor/layers/quantization/exl3.py"
EXL3_MARKER = "# b12x-latest-api: scratch-planned fused MoE"

EXL3_LOADER_OLD = '''def _load_b12x_fused_moe() -> Any:
    """Resolve the public unified MoE API lazily."""

    global _SPARKINFER_FUSED_MOE_API
    if _SPARKINFER_FUSED_MOE_API is not None:
        return _SPARKINFER_FUSED_MOE_API
    try:
        from b12x.moe import fused_moe
    except Exception as exc:
        raise RuntimeError(
            "Rank-sliced EXL3 requires the b12x_trellis MCG source in "
            "b12x.moe.fused_moe. Install the recipe-pinned B12x build."
        ) from exc
    _SPARKINFER_FUSED_MOE_API = fused_moe
    return fused_moe
'''
EXL3_LOADER_NEW = '''def _load_b12x_fused_moe() -> Any:
    """Resolve B12x's scratch-planned fused-MoE layer lazily.

    Current B12x removed the vLLM compatibility shim (``quant_modes``
    planning, ``Caps``/``plan``/``required_nbytes``) that this adapter used.
    Projection-mixed tiers, EP route maps, BF16 epilogues and K2 uniform
    layers are not expressible through ``plan_execution``, so bind the same
    scratch-planned ``_impl`` layer the shim wrapped. Plans still compile
    their Trellis launches eagerly at construction, before graph capture.
    """

    # b12x-latest-api: scratch-planned fused MoE
    global _SPARKINFER_FUSED_MOE_API
    if _SPARKINFER_FUSED_MOE_API is not None:
        return _SPARKINFER_FUSED_MOE_API
    try:
        from b12x.moe.fused_moe import _impl
        from b12x.moe.fused_moe._tuning import MoeDecodeConfig
    except Exception as exc:
        raise RuntimeError(
            "Rank-sliced EXL3 requires the b12x_trellis MCG source in "
            "b12x.moe.fused_moe. Install the recipe-pinned B12x build."
        ) from exc

    # TPMoEScratchCaps now takes a concrete decode configuration instead of
    # resolving a policy. Trellis W4A16 is ineligible for direct routing, so
    # B12x's heuristic (and the pinned fork's policy) select exactly this.
    trellis_decode_config = MoeDecodeConfig(
        backend="w4a16",
        route_planner="internal",
        max_active_clusters=None,
        w4a16_route_mode="packed",
    )

    def prepare_weights(
        *,
        plan: Any,
        params_dtype: torch.dtype,
        projection_tiers: Any = None,
        **kwargs: Any,
    ) -> Any:
        if projection_tiers is not None:
            return _impl.prepare_b12x_projection_native_trellis_weights(
                plan=plan,
                native_tiers=projection_tiers,
                gate_suh=kwargs["gate_suh"],
                up_suh=kwargs["up_suh"],
                intermediate_rotations=kwargs["intermediate_rotations"],
                down_svh=kwargs["down_svh"],
            )
        return _impl.prepare_b12x_fp4_moe_weights(
            plan=plan, params_dtype=params_dtype, **kwargs
        )

    def caps(**kwargs: Any) -> Any:
        weight_plan = kwargs["weight_plan"]
        if weight_plan.source_format not in ("b12x_trellis", "exl3_trellis_mcg"):
            raise RuntimeError(
                "EXL3 fused MoE decode config is qualified only for Trellis "
                f"sources, got {weight_plan.source_format!r}"
            )
        kwargs.setdefault("decode_config", trellis_decode_config)
        return _impl.TPMoEScratchCaps(**kwargs)

    api = SimpleNamespace(
        plan_weights=_impl.plan_b12x_fp4_moe_weights,
        prepare_weights=prepare_weights,
        Caps=caps,
        plan=_impl.plan_tp_moe_scratch,
        required_nbytes=_impl.tp_moe_required_nbytes,
        bind=lambda plan, **kwargs: plan.bind(**kwargs),
        run=lambda *, binding: _impl.b12x_moe_fp4(binding=binding),
    )
    _SPARKINFER_FUSED_MOE_API = api
    return api
'''
EXL3_GEMV_OLD = """    def process_weights_after_loading(self, layer: Any) -> None:
        super().process_weights_after_loading(layer)
        from b12x.gemm import bf16_gemv

        weight = layer.weight
        if weight.ndim == 2 and weight.is_cuda and weight.dtype == torch.bfloat16:
            layer.b12x_gemv_weight = weight.data.detach().clone().contiguous()
            bf16_gemv.precompile(layer.b12x_gemv_weight)

    def apply(
        self, layer: Any, x: torch.Tensor, bias: torch.Tensor | None = None
    ) -> torch.Tensor:
        weight = getattr(layer, "b12x_gemv_weight", None)
        if (
            weight is not None
            and bias is None
            and x.dtype == torch.bfloat16
            and weight.dtype == torch.bfloat16
        ):
            from b12x.gemm import bf16_gemv

            x_2d = x.reshape(-1, x.shape[-1])
            output = bf16_gemv.mm(x_2d, weight)
            return output.reshape(*x.shape[:-1], weight.shape[0])
        return super().apply(layer, x, bias)
"""
EXL3_GEMV_NEW = """    def process_weights_after_loading(self, layer: Any) -> None:
        super().process_weights_after_loading(layer)
        from b12x.gemm import bf16_gemv
        from b12x.preparation import PreparedCall

        from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

        weight = layer.weight
        if weight.ndim == 2 and weight.is_cuda and weight.dtype == torch.bfloat16:
            # b12x-latest-api: bf16_gemv.precompile was removed and mm now
            # requires a prepared plan. Declare the decode rows the pinned
            # fork compiled (m <= SMALL_M_MAX) and prime them before capture;
            # larger m keeps the pinned fork's F.linear fallback.
            gemv_weight = weight.data.detach().clone().contiguous()
            probe = torch.zeros(
                (int(bf16_gemv.SMALL_M_MAX), int(gemv_weight.shape[1])),
                dtype=torch.bfloat16,
                device=gemv_weight.device,
            )
            try:
                query = bf16_gemv.query_from_call(probe, gemv_weight)
            except ValueError as exc:
                logger.info(
                    "B12x BF16 GEMV unsupported for weight %s: %s",
                    tuple(gemv_weight.shape),
                    exc,
                )
                return
            plan = bf16_gemv.plan(query)
            prepare_b12x_plan(
                plan,
                name=(
                    "vllm.exl3.bf16_gemv."
                    f"{int(gemv_weight.shape[0])}x{int(gemv_weight.shape[1])}"
                ),
                prepare_call=lambda state: PreparedCall(
                    run=lambda: state.run(probe, gemv_weight)
                ),
            )
            layer.b12x_gemv_weight = gemv_weight
            # Plan handles resolve through a weak registry; hold it here.
            layer.b12x_gemv_plan = plan

    def apply(
        self, layer: Any, x: torch.Tensor, bias: torch.Tensor | None = None
    ) -> torch.Tensor:
        weight = getattr(layer, "b12x_gemv_weight", None)
        plan = getattr(layer, "b12x_gemv_plan", None)
        if (
            weight is not None
            and plan is not None
            and bias is None
            and x.dtype == torch.bfloat16
            and weight.dtype == torch.bfloat16
        ):
            x_2d = x.reshape(-1, x.shape[-1])
            if 0 < x_2d.shape[0] <= int(plan.query.max_rows):
                if not x_2d.is_contiguous() or x_2d.data_ptr() % 16 != 0:
                    x_2d = x_2d.contiguous()
                if x_2d.data_ptr() % 16 == 0:
                    from b12x.gemm import bf16_gemv

                    output = bf16_gemv.mm(x_2d, weight, plan=plan)
                    return output.reshape(*x.shape[:-1], weight.shape[0])
            output = torch.nn.functional.linear(x_2d, weight)
            return output.reshape(*x.shape[:-1], weight.shape[0])
        return super().apply(layer, x, bias)
"""
EXL3_UNIFORM_PLAN_OLD = """            source_format="b12x_trellis",
            activation=layer.activation.value,
            params_dtype=torch.float16,
"""
EXL3_UNIFORM_PLAN_NEW = """            source_format="b12x_trellis",
            activation=layer.activation.value,
            # Current B12x compiles the full-rotation launch for the plan's
            # I/O dtype and always keeps FP16 prepared operands internally
            # (41e51855), so the plan follows vLLM's BF16 activations.
            params_dtype=layer.exl3_params_dtype,
"""
EXL3_UNIFORM_PREP_OLD = """            plan=weight_plan,
            params_dtype=torch.float16,
            w1_fp4=w13,
"""
EXL3_UNIFORM_PREP_NEW = """            plan=weight_plan,
            params_dtype=layer.exl3_params_dtype,
            w1_fp4=w13,
"""
EXL3_EP_MAP_OLD = """        if layer.use_ep:
            ep_api = _load_b12x_ep_moe()
            layer.exl3_prepared_ep_map = ep_api.prepare_expert_map(
                layer.exl3_ep_expert_map_tensor,
                local_num_experts=num_experts,
                global_num_experts=int(layer.exl3_global_num_experts),
                device=w13.device,
            )
"""
EXL3_EP_MAP_NEW = """        layer.exl3_projection_route_map = None
        if layer.use_ep:
            # Current B12x ep_moe no longer compiles full-rotation Trellis.
            # The fused path consumes the static global->local map directly
            # and drops non-local (-1) routes in the kernel and top-k sum.
            layer.exl3_projection_route_map = (
                layer.exl3_ep_expert_map_tensor.to(
                    device=w13.device, dtype=torch.int32
                ).contiguous()
            )
"""
EXL3_API_OLD = """        api = (
            _load_b12x_fused_moe()
            if projection_mixed
            else (_load_b12x_ep_moe() if use_ep else _load_b12x_fused_moe())
        )
"""
EXL3_API_NEW = """        api = _load_b12x_fused_moe()
"""
EXL3_CAPS_OLD = """            if use_ep and not projection_mixed:
                caps = api.Caps(
                    max_tokens=plan_max_tokens,
                    num_topk=topk,
                    global_num_experts=int(layer.exl3_global_num_experts),
                    device=x.device,
                    weight_plan=layer.exl3_trellis_weights.plan,
                )
            else:
                caps = api.Caps(
                    max_tokens=plan_max_tokens,
                    num_topk=topk,
                    # Projection-mixed EP supplies one immutable map from
                    # global routes to rank-local tier slots.
                    route_num_experts=(
                        int(layer.exl3_global_num_experts)
                        if projection_ep
                        else 0
                    ),
                    device=x.device,
                    weight_plan=layer.exl3_trellis_weights.plan,
                    quant_mode="w4a16",
                    w4a16_block_size_m=plan_block_m,
                    full_rotation_output_dtype=(
                        torch.bfloat16
                        if bf16_epilogue
                        else torch.float32
                    ),
                    **mixed_launch_caps,
                )
"""
EXL3_CAPS_NEW = """            caps = api.Caps(
                max_tokens=plan_max_tokens,
                num_topk=topk,
                # EP supplies one immutable map from global routes to
                # rank-local experts (uniform) or tier slots (projection).
                route_num_experts=(
                    int(layer.exl3_global_num_experts) if use_ep else 0
                ),
                device=x.device,
                weight_plan=layer.exl3_trellis_weights.plan,
                quant_mode="w4a16",
                w4a16_block_size_m=plan_block_m,
                full_rotation_output_dtype=(
                    torch.bfloat16
                    if bf16_epilogue
                    else torch.float32
                ),
                **mixed_launch_caps,
            )
"""
EXL3_OUTPUT_OLD = """                    dtype=torch.bfloat16,
                    device=x.device,
                )
                if use_ep
                else None
            )
            return plan, scratch, output
"""
EXL3_OUTPUT_NEW = """                    dtype=torch.bfloat16,
                    device=x.device,
                )
                # Full-rotation plans return their scratch-owned output.
                if projection_ep
                else None
            )
            return plan, scratch, output
"""
EXL3_BIND_OLD = """        if runtime["use_ep"]:
            if output is None:
                raise RuntimeError("EXL3 EP runtime has no stable output buffer")
            if runtime["projection_ep"]:
                kwargs.update(
                    route_expert_map=layer.exl3_projection_route_map,
                    output=output[: x.shape[0]],
                )
            else:
                kwargs.update(
                    expert_map=layer.exl3_prepared_ep_map,
                    output=output[: x.shape[0]],
                )
"""
EXL3_BIND_NEW = """        if runtime["use_ep"]:
            kwargs["route_expert_map"] = layer.exl3_projection_route_map
            if runtime["projection_ep"]:
                if output is None:
                    raise RuntimeError(
                        "EXL3 EP runtime has no stable output buffer"
                    )
                kwargs["output"] = output[: x.shape[0]]
"""


def _port_exl3(source: str) -> str:
    where = EXL3
    source = _replace_once(source, EXL3_LOADER_OLD, EXL3_LOADER_NEW, where=where)
    source = _replace_once(source, EXL3_GEMV_OLD, EXL3_GEMV_NEW, where=where)
    start_anchor = "    def _prepare_rank_sliced_weights("
    end_anchor = "    def get_fused_moe_quant_config("
    if source.count(start_anchor) != 1:
        raise Drift(f"{where}: uniform preparation section drift")
    start = source.index(start_anchor)
    end = source.index(end_anchor, start)
    section = source[start:end]
    for old, new in (
        (EXL3_UNIFORM_PLAN_OLD, EXL3_UNIFORM_PLAN_NEW),
        (EXL3_UNIFORM_PREP_OLD, EXL3_UNIFORM_PREP_NEW),
        (EXL3_EP_MAP_OLD, EXL3_EP_MAP_NEW),
    ):
        section = _replace_once(section, old, new, where=f"{where}:uniform")
    source = source[:start] + section + source[end:]
    for old, new in (
        (EXL3_API_OLD, EXL3_API_NEW),
        (EXL3_CAPS_OLD, EXL3_CAPS_NEW),
        (EXL3_OUTPUT_OLD, EXL3_OUTPUT_NEW),
        (EXL3_BIND_OLD, EXL3_BIND_NEW),
    ):
        source = _replace_once(source, old, new, where=where)
    for removed in (
        "bf16_gemv.precompile(",
        "exl3_prepared_ep_map",
        "global_num_experts=int(layer.exl3_global_num_experts),\n                    device",
    ):
        if removed in source:
            raise Drift(f"{where}: stale B12x API reference remains: {removed!r}")
    return source


def main() -> None:
    _write_helper()
    _patch(SPARSE_MLA, SPARSE_MLA_MARKER, _port_sparse_mla)
    _patch(INDEXER, INDEXER_MARKER, _port_indexer)
    _patch(ALL_REDUCE, ALL_REDUCE_MARKER, _port_all_reduce)
    _patch(DCP_A2A, DCP_A2A_MARKER, _port_dcp_a2a)
    _patch(MHC, MHC_MARKER, _port_mhc)
    _patch(EXL3, EXL3_MARKER, _port_exl3)
    print("B12x call sites ported to the prepared plan/bind/run API")


if __name__ == "__main__":
    try:
        main()
    except Drift as error:
        raise SystemExit(f"port-b12x-latest-api: source drift: {error}") from None
