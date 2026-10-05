#!/usr/bin/env python3
"""Size the kpool tail ring for speculative decoding (vLLM PR #58454).

The tail ring held exactly ``index_kpool`` slots per request. Drafts are stashed
before acceptance, so when a pool-completing draft is rejected, the drafts behind
it have already overwritten that pool's committed keys (``pos % kpool`` wraps
onto them) and the redo compresses the pool from corrupted slots. Affects any
speculative method with ``num_speculative_tokens >= 2``, once context exceeds
``index_topk``.

The ring now holds ``kpool * next_power_of_2(cdiv(kpool + num_spec, kpool))``
slots (8 for 3 drafts, 16 for 5-7). The tail slot mapping, cache shape,
allocator and page size all derive from the spec's ``block_size``, so only the
spec and the two tail kernels change. Apply after port-kpool-seed-stride-glm53.py
(vLLM #57477), as upstream did. Usage: port-kpool-spec-ring-glm53.py VLLM_DIR
"""

import sys
from pathlib import Path

vllm = Path(sys.argv[1])


PENDING: list[tuple[Path, str]] = []


def patch(rel: str, edits: list[tuple[str, str, int]], marker: str) -> None:
    """Validate every edit before anything is written (see the end of the file)."""
    path = vllm / rel
    source = path.read_text()
    if marker in source:
        return
    for old, new, count in edits:
        if source.count(old) != count:
            raise RuntimeError(f"{rel}: source drift ({source.count(old)} != {count}):\n{old}")
        source = source.replace(old, new)
    compile(source, str(path), "exec")
    PENDING.append((path, source))


# --- 1. the spec: ring-sized block --------------------------------------------
patch(
    "models/glm5next/nvidia/attention.py",
    [
        (
            """        return KpoolTailSpec(
            block_size=self._index_kpool,
            num_kv_heads=1,
            head_size=2 * self.head_dim,
            head_size_v=0,
            dtype=torch.bfloat16,
            sliding_window=self._index_kpool,
        )""",
            """        # kpool-spec-ring: drafts are stashed before acceptance. With a one-pool
        # ring, the drafts behind a rejected pool-completing draft overwrite the
        # keys its redo reads. Size the ring for one pool plus every draft.
        kpool = self._index_kpool
        n = -(-(kpool + vllm_config.num_speculative_tokens) // kpool)
        ring = kpool * (1 << (n - 1).bit_length())
        assert vllm_config.cache_config.block_size % ring == 0, (
            f"Glm5NextTailCache: cache block_size "
            f"({vllm_config.cache_config.block_size}) must be a multiple of the "
            f"tail ring ({ring})"
        )
        return KpoolTailSpec(
            block_size=ring,
            num_kv_heads=1,
            head_size=2 * self.head_dim,
            head_size_v=0,
            dtype=torch.bfloat16,
            sliding_window=ring,
        )""",
            1,
        ),
    ],
    marker="kpool-spec-ring",
)

# --- 2. the kernels: address the ring by RING, not KPOOL ----------------------
K = "models/glm5next/nvidia/ops/kpool_compress.py"
patch(
    K,
    [
        # seed kernel (prefill writes the trailing incomplete pool)
        (
            """    HEAD_DIM: tl.constexpr,
    KPOOL: tl.constexpr,
    BLOCK_D: tl.constexpr,
):
    \"\"\"Copy token ``i``'s raw K + gate into its request's tail block.""",
            """    HEAD_DIM: tl.constexpr,
    KPOOL: tl.constexpr,
    RING: tl.constexpr,
    BLOCK_D: tl.constexpr,
):
    \"\"\"Copy token ``i``'s raw K + gate into its request's tail ring (kpool-spec-ring).""",
            1,
        ),
        ("""slot < 0). ``tslot = block * KPOOL + pos % KPOOL``; the destination is
    ``tail[block, {0:K, 1:score}, pos % KPOOL, :]``.""",
         """slot < 0). ``tslot = block * RING + pos % RING``; the destination is
    ``tail[block, {0:K, 1:score}, pos % RING, :]``.""", 1),
        ("    blk = t // KPOOL  # t >= 0 here, so trunc == floor\n",
         "    blk = t // RING  # t >= 0 here, so trunc == floor\n", 1),
        ("    if ahead >= 0 and ahead // KPOOL == blk:\n",
         "    if ahead >= 0 and ahead // RING == blk:\n", 1),
        ("    base = blk * TAIL_BLOCK_ELEMS + (t % KPOOL) * HEAD_DIM\n",
         "    base = blk * TAIL_BLOCK_ELEMS + (t % RING) * HEAD_DIM\n", 1),
        # seed wrapper: the kernel assumes a contiguous [blocks, 2, RING, D] ring
        (
            """    n = tslot.shape[0]
    if n == 0:
        return
    _kpool_tail_seed_kernel[(n,)](""",
            """    ring = tail_kv_cache.shape[2]
    assert ring >= kpool and ring % kpool == 0, (ring, kpool)
    n = tslot.shape[0]
    if n == 0:
        return
    _kpool_tail_seed_kernel[(n,)](""",
            1,
        ),
        ("""        KPOOL=kpool,
        BLOCK_D=triton.next_power_of_2(head_dim),""",
         """        KPOOL=kpool,
        RING=ring,
        BLOCK_D=triton.next_power_of_2(head_dim),""", 1),
        # decode kernel (spec verify stashes, completes and compresses pools)
        (
            """    POOL_SIZE: tl.constexpr,
    TAIL_BLOCK_ELEMS: tl.constexpr,""",
            """    POOL_SIZE: tl.constexpr,
    RING: tl.constexpr,
    TAIL_BLOCK_ELEMS: tl.constexpr,""",
            1,
        ),
        (
            """    programs are independent (distinct tail blocks). With NEXT_N < POOL_SIZE
    (the spec-verify case: NEXT_N ~= num_spec+1, POOL_SIZE=16) at most one
    completion can occur per request per call, but the ordered loop is correct
    for any NEXT_N.""",
            """    programs are independent (distinct tail blocks). RING >= POOL_SIZE and
    holds a pool plus every draft, so a rejected pool-completing draft cannot
    let later drafts overwrite that pool's committed keys (kpool-spec-ring).""",
            1,
        ),
        ("        phys_slot = safe_pos % POOL_SIZE\n", "        phys_slot = safe_pos % RING\n", 1),
        ("        block = tl.maximum(tail_slot, 0).to(tl.int64) // POOL_SIZE\n",
         "        block = tl.maximum(tail_slot, 0).to(tl.int64) // RING\n", 1),
        ("                phys = (pool_logical_start + pool_slot) % POOL_SIZE\n",
         "                phys = (pool_logical_start + pool_slot) % RING\n", 2),
        # decode wrapper
        ("    assert tail_kv_cache.shape[2] == pool_size\n",
         "    ring = tail_kv_cache.shape[2]\n    assert ring >= pool_size and ring % pool_size == 0, (ring, pool_size)\n", 1),
        ("""        POOL_SIZE=pool_size,
        TAIL_BLOCK_ELEMS=tail_kv_cache.stride(0),""",
         """        POOL_SIZE=pool_size,
        RING=ring,
        TAIL_BLOCK_ELEMS=tail_kv_cache.stride(0),""", 1),
    ],
    marker="kpool-spec-ring",
)

for path, source in PENDING:
    path.write_text(source)

print("GLM kpool tail ring sized for speculative drafts (vLLM #58454)")
