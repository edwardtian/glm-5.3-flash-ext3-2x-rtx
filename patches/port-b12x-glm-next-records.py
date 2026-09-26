#!/usr/bin/env python3
"""Opt-in GLM_NEXT 528-byte no-RoPE MLA cache records for GLM-5.3.

GLM-5.3 is NoPE (``qk_rope_head_dim == 0``). The released recipe stores its
MLA latent in the DeepSeek V3.2 ``fp8_ds_mla`` record (656 bytes: 512 E4M3
latent bytes, four FP32 group scales, and 128 bytes of BF16 RoPE that are
always zero) and pads every absorbed query from 512 to 576 lanes so B12x can
run its GLM_NSA kernels. B12x now carries an explicit GLM_NEXT recipe with a
528-byte record (the same 512 E4M3 latent bytes and four FP32 group-128
scales, no RoPE tail) and a 512-wide query.

``VLLM_GLM53_NOPE_RECORD=1`` selects that recipe; it is off by default, so
an image built with this patch keeps the 656-byte behavior unless the
variable is set in every vLLM process. The switch is read once at import.
The ``fp8_ds_mla`` cache dtype string is kept; only the record changes:

* KV spec page bytes and the backend cache shape: 528 bytes per token for
  the 512-wide GLM MLA spec (head_size 512). The hybrid layout's MLA block is
  re-derived by vLLM's own page-size unification.
* Sparse-MLA plans: ``ModelType.GLM_NEXT``, 512-wide query, 528-byte records,
  unchanged physical softmax scale.
* Cache writes: B12x's prepared ``concat_and_cache_glm_next_mla`` writer.
* The 576-only fused H64 query projection is bypassed (plain BMM).
* A distinct record-ABI identity, mixed into the cache-config hash.

FP8 only: ``nvfp4_ds_mla`` with this switch raises NotImplementedError, and
DCP > 1 raises ValueError (this layout targets DCP=1, e.g. MLA layer
ownership).

Every edit is an exact replace-once anchor; the script is idempotent, fails
closed on drift, and compiles each rewritten module before writing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(sys.argv[1])


class Drift(RuntimeError):
    pass


def _patch(relative: str, marker: str, edits) -> None:
    path = ROOT / relative
    source = path.read_text()
    if marker in source:
        print(f"[skip] {relative}: already ported")
        return
    for old, new in edits:
        count = source.count(old)
        if count != 1:
            raise Drift(
                f"{relative}: expected one anchor, found {count}: {old[:120]!r}"
            )
        source = source.replace(old, new)
    if marker not in source:
        raise Drift(f"{relative}: port marker missing after rewrite")
    compile(source, str(path), "exec")
    path.write_text(source)
    print(f"[ok]   {relative}")


MARKER = "glm53-nope-record"

# ---------------------------------------------------------------------------
# 1. Record identity (single process-wide switch).
# ---------------------------------------------------------------------------
FORMAT = "model_executor/layers/mla_cache_format.py"
FORMAT_EDITS = (
    (
        'KV_FP8_ROPE_ENV = "KV_FP8_ROPE"\n',
        '''KV_FP8_ROPE_ENV = "KV_FP8_ROPE"

# glm53-nope-record: GLM-5.3 NoPE MLA latent stored in B12x's GLM_NEXT
# 528-byte record (512 E4M3 latent bytes + 4 FP32 group-128 scales, no RoPE
# tail) instead of the 656-byte fp8_ds_mla record. Opt-in, read once at
# import like the NVFP4 format so writer, readers and page sizing agree.
GLM53_NOPE_RECORD_ENV = "VLLM_GLM53_NOPE_RECORD"
GLM53_NOPE_RECORD = os.getenv(GLM53_NOPE_RECORD_ENV, "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
GLM53_NOPE_RECORD_BYTES = 528
# GLM_NEXT applies to the 512-wide (kv_lora_rank=512, rope=0) MLA spec only.
GLM53_NOPE_RECORD_HEAD_SIZE = 512
GLM53_NOPE_RECORD_ABI = "fp8_ds_mla:glm-next-nope-528:v1"


def glm53_nope_record_bytes(cache_dtype: str | None, head_size: int) -> int | None:
    """Record bytes when the GLM_NEXT record replaces fp8_ds_mla, else None."""
    if (
        GLM53_NOPE_RECORD
        and str(cache_dtype).replace("torch.", "") == "fp8_ds_mla"
        and int(head_size) == GLM53_NOPE_RECORD_HEAD_SIZE
    ):
        return GLM53_NOPE_RECORD_BYTES
    return None
''',
    ),
    (
        '''        normalized_dtype = str(cache_dtype).replace("torch.", "")
        if normalized_dtype != "nvfp4_ds_mla":
            return "vllm-default-v1"
''',
        '''        normalized_dtype = str(cache_dtype).replace("torch.", "")
        if GLM53_NOPE_RECORD and normalized_dtype == "fp8_ds_mla":
            return GLM53_NOPE_RECORD_ABI
        if normalized_dtype != "nvfp4_ds_mla":
            return "vllm-default-v1"
''',
    ),
)

# ---------------------------------------------------------------------------
# 2. KV spec page bytes (drives the hybrid MLA/KDA block-size unification,
#    the kv-cache tensor sizing and the draft slot-sharing page fit).
# ---------------------------------------------------------------------------
KV_INTERFACE = "v1/kv_cache_interface.py"
KV_INTERFACE_EDITS = (
    (
        """            # V3.2 main MLA: 656-byte custom layout (kv_lora_rank=512 +
            # qk_rope_head_dim=64, head_size=576). See flashmla_sparse.py.
            return self.block_size * 656
""",
        """            from vllm.model_executor.layers.mla_cache_format import (
                glm53_nope_record_bytes,
            )

            # glm53-nope-record: GLM-5.3's 512-wide NoPE latent in the
            # B12x GLM_NEXT record when VLLM_GLM53_NOPE_RECORD=1.
            glm_next_bytes = glm53_nope_record_bytes(
                self.cache_dtype_str, self.head_size
            )
            if glm_next_bytes is not None:
                return self.block_size * glm_next_bytes
            # V3.2 main MLA: 656-byte custom layout (kv_lora_rank=512 +
            # qk_rope_head_dim=64, head_size=576). See flashmla_sparse.py.
            return self.block_size * 656
""",
    ),
)

# ---------------------------------------------------------------------------
# 3. Cache-config hash: compiled artifacts and anything keyed by it cannot be
#    shared across record formats. Default (switch off) hash is unchanged.
# ---------------------------------------------------------------------------
CACHE_CONFIG = "config/cache.py"
CACHE_CONFIG_EDITS = (
    (
        """        factors = get_hash_factors(self, ignored_factors)
        return hash_factors(factors)
""",
        """        factors = get_hash_factors(self, ignored_factors)
        from vllm.model_executor.layers.mla_cache_format import (
            GLM53_NOPE_RECORD,
            GLM53_NOPE_RECORD_ABI,
        )

        # glm53-nope-record: the MLA record format is part of the cache ABI.
        if GLM53_NOPE_RECORD and self.cache_dtype == "fp8_ds_mla":
            factors["glm53_mla_record_abi"] = GLM53_NOPE_RECORD_ABI
        return hash_factors(factors)
""",
    ),
)

# ---------------------------------------------------------------------------
# 4. B12x sparse-MLA backend.
# ---------------------------------------------------------------------------
BACKEND = "v1/attention/backends/mla/b12x_mla_sparse.py"
BACKEND_EDITS = (
    (
        """from vllm.model_executor.layers.mla_cache_format import (
    NVFP4_MLA_CACHE_FORMAT,
)
""",
        """from vllm.model_executor.layers.mla_cache_format import (
    GLM53_NOPE_RECORD,
    GLM53_NOPE_RECORD_BYTES,
    NVFP4_MLA_CACHE_FORMAT,
    glm53_nope_record_bytes,
)
""",
    ),
    (
        """_KV_FP8_ROPE_WRITER_PREWARMED: set[tuple[int | None, int, bool]] = set()
""",
        '''_KV_FP8_ROPE_WRITER_PREWARMED: set[tuple[int | None, int, bool]] = set()
# glm53-nope-record: one prepared GLM_NEXT cache writer per (device, page).
_GLM_NEXT_WRITER_PLANS: dict[tuple[int | None, int], Any] = {}


def _prepare_glm_next_writer_plan(device: torch.device, page_size: int) -> Any:
    """Declare and prime the exact GLM_NEXT 528-byte cache writer.

    The writer specializes on the device, page size, latent dtype and slot
    dtype only (token and page counts are runtime values), so a plan built on
    one-row, one-page dummies serves every live cache. Priming launches the
    kernel with a skipped slot (-1): it compiles and loads the module before
    any CUDA graph capture and writes nothing.
    """
    key = (device.index, int(page_size))
    plan = _GLM_NEXT_WRITER_PLANS.get(key)
    if plan is not None:
        return plan
    from b12x.attention import sparse_mla
    from b12x.preparation import PreparedCall

    from vllm.model_executor.layers.b12x_preparation import prepare_b12x_plan

    kv_c = torch.zeros((1, 512), dtype=torch.bfloat16, device=device)
    kv_cache = torch.zeros(
        (1, int(page_size), GLM53_NOPE_RECORD_BYTES),
        dtype=torch.uint8,
        device=device,
    )
    slots = torch.full((1,), -1, dtype=torch.int64, device=device)
    plan = sparse_mla.plan_cache_writer(kv_c, kv_cache, slots)
    prepare_b12x_plan(
        plan,
        name=f"vllm.b12x_mla_sparse.glm_next_writer.page{int(page_size)}",
        prepare_call=lambda state: PreparedCall(
            run=lambda: state.run(kv_c, kv_cache, slots)
        ),
    )
    _GLM_NEXT_WRITER_PLANS[key] = plan
    return plan
''',
    ),
    (
        """            return (num_blocks, block_size, 656)
        if cache_dtype_str == "nvfp4_ds_mla":
""",
        """            glm_next_bytes = glm53_nope_record_bytes(cache_dtype_str, head_size)
            if glm_next_bytes is not None:
                # glm53-nope-record: B12x GLM_NEXT record (512 E4M3 latent
                # bytes + 4 FP32 group scales, no RoPE tail).
                return (num_blocks, block_size, glm_next_bytes)
            return (num_blocks, block_size, 656)
        if cache_dtype_str == "nvfp4_ds_mla":
""",
    ),
    (
        """        self.rope_pad = 0
        if self.qk_rope_head_dim == 0:
            if self.kv_lora_rank != 512:
""",
        """        self.rope_pad = 0
        # glm53-nope-record: GLM_NEXT consumes the 512-wide NoPE query
        # directly, so no zero RoPE lanes are appended.
        self._glm_next_record = bool(GLM53_NOPE_RECORD)
        if self._glm_next_record and (
            self.qk_rope_head_dim != 0 or self.kv_lora_rank != 512
        ):
            raise ValueError(
                "VLLM_GLM53_NOPE_RECORD=1 requires a NoPE MLA with "
                "kv_lora_rank=512 and qk_rope_head_dim=0; got "
                f"kv_lora_rank={self.kv_lora_rank}, "
                f"qk_rope_head_dim={self.qk_rope_head_dim}"
            )
        if self.qk_rope_head_dim == 0 and not self._glm_next_record:
            if self.kv_lora_rank != 512:
""",
    ),
    (
        """        self._kv_record_bytes = (
            (368 if self._kv_fp8_rope else 432)
            if self.kv_cache_dtype == "nvfp4_ds_mla"
            else 656
        )
""",
        """        self._kv_record_bytes = (
            (368 if self._kv_fp8_rope else 432)
            if self.kv_cache_dtype == "nvfp4_ds_mla"
            else 656
        )
        if self._glm_next_record:
            if self.kv_cache_dtype != "fp8_ds_mla":
                raise NotImplementedError(
                    "VLLM_GLM53_NOPE_RECORD=1 supports kv_cache_dtype="
                    "fp8_ds_mla only (the 304-byte NVFP4 GLM_NEXT record is "
                    f"not wired); got {self.kv_cache_dtype!r}"
                )
            if self.dcp_world_size > 1:
                raise ValueError(
                    "VLLM_GLM53_NOPE_RECORD=1 requires DCP=1 (MLA layer "
                    "ownership or plain TP); the DCP query gather, LSE "
                    "combine and CKV gather use the 576-wide GLM_NSA layout. "
                    f"Got decode_context_parallel_size={self.dcp_world_size}"
                )
            self._kv_record_bytes = GLM53_NOPE_RECORD_BYTES
""",
    ),
    (
        """        glm_nsa = int(b12x_sparse_mla.ModelType.GLM_NSA)
""",
        """        # glm53-nope-record: explicit GLM_NEXT identity for 512-wide queries
        # (the 512 width would otherwise resolve to DSV4).
        glm_nsa = int(
            b12x_sparse_mla.ModelType.GLM_NEXT
            if self._glm_next_record
            else b12x_sparse_mla.ModelType.GLM_NSA
        )
""",
    ),
    (
        """        self._extend_plan = _make_plan(
            "extend", max_batched, self._kernel_num_heads, max_num_seqs
        )
""",
        """        self._extend_plan = _make_plan(
            "extend", max_batched, self._kernel_num_heads, max_num_seqs
        )
        self._glm_next_writer_plan = (
            _prepare_glm_next_writer_plan(self.device, self.block_size)
            if self._glm_next_record and self.device.type == "cuda"
            else None
        )
""",
    ),
    (
        """        # The packed GLM_NSA record reserves a BF16 RoPE tail even
        # for GLM-5.3 NoPE. Keep it exactly zero.
        if self.rope_pad:
""",
        """        if self._glm_next_record:
            # glm53-nope-record: quantize the NoPE latent straight into the
            # 528-byte GLM_NEXT record (no RoPE payload, k_pe is empty).
            if kv_cache.numel() == 0:
                return
            if self._glm_next_writer_plan is None:
                raise RuntimeError("GLM_NEXT cache writer plan was not prepared")
            self._b12x_sparse_mla.concat_and_cache_glm_next_mla(
                kv_c_normed,
                kv_cache.view(torch.uint8),
                slot_mapping.flatten(),
                plan=self._glm_next_writer_plan,
            )
            return
        # The packed GLM_NSA record reserves a BF16 RoPE tail even
        # for GLM-5.3 NoPE. Keep it exactly zero.
        if self.rope_pad:
""",
    ),
    (
        """            if self.rope_pad:
                q_all.zero_()
                q_all[..., : ql_nope.shape[-1]].copy_(ql_nope)
            else:
                ops.concat_mla_q(ql_nope, q_pe, q_all)
""",
        """            if self.rope_pad:
                q_all.zero_()
                q_all[..., : ql_nope.shape[-1]].copy_(ql_nope)
            elif q_pe.shape[-1] == 0:
                # glm53-nope-record: 512-wide GLM_NEXT query, no RoPE lanes.
                q_all.copy_(ql_nope)
            else:
                ops.concat_mla_q(ql_nope, q_pe, q_all)
""",
    ),
)

# ---------------------------------------------------------------------------
# 5. MLA layer: the fused H64 query projection writes only the 576-wide
#    GLM_NSA layout. Bypass it (plain BMM) when the backend plans 512.
# ---------------------------------------------------------------------------
MLA_ATTENTION = "model_executor/layers/attention/mla_attention.py"
MLA_ATTENTION_EDITS = (
    (
        """                b12x_plan = None
                if self.attn_backend.get_name() == "B12X_MLA_SPARSE":
                    from b12x.gemm import mla_query_projection
""",
        """                b12x_plan = None
                # glm53-nope-record: the H64 projection only emits the
                # 576-wide (512 + 64 zero RoPE) query layout.
                if (
                    self.attn_backend.get_name() == "B12X_MLA_SPARSE"
                    and getattr(self.impl, "q_head_dim", L + 64) == L + 64
                ):
                    from b12x.gemm import mla_query_projection
""",
    ),
    (
        """    def _prewarm_b12x_glm_h64_query_projection(self) -> None:
        if self.attn_backend.get_name() != "B12X_MLA_SPARSE":
            return
""",
        """    def _prewarm_b12x_glm_h64_query_projection(self) -> None:
        if self.attn_backend.get_name() != "B12X_MLA_SPARSE":
            return
        # glm53-nope-record: nothing to prewarm for the 512-wide query.
        if getattr(self.impl, "q_head_dim", 576) != 576:
            return
""",
    ),
)


def main() -> None:
    _patch(FORMAT, MARKER, FORMAT_EDITS)
    _patch(KV_INTERFACE, MARKER, KV_INTERFACE_EDITS)
    _patch(CACHE_CONFIG, MARKER, CACHE_CONFIG_EDITS)
    _patch(BACKEND, MARKER, BACKEND_EDITS)
    _patch(MLA_ATTENTION, MARKER, MLA_ATTENTION_EDITS)
    print("GLM_NEXT 528-byte MLA records available (VLLM_GLM53_NOPE_RECORD=1)")


if __name__ == "__main__":
    try:
        main()
    except Drift as error:
        raise SystemExit(f"port-b12x-glm-next-records: source drift: {error}") from None
