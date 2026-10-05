#!/usr/bin/env python3
"""Address kpool tail blocks by the padded stride in the prefill seed kernel (vLLM PR #57477).

The tail cache aliases the indexer cache with the indexer's padded block stride
(``tail.stride(0)`` is far larger than a dense ``2 * kpool * head_dim``). The
seed kernel addressed it densely, so every prefill left its own tail block
unseeded and wrote 2 KB of raw bf16 K / gate rows into another block's indexer
region. The decode kernel already addresses the tail through its strides.
Usage: port-kpool-seed-stride-glm53.py VLLM_DIR
"""

import sys
from pathlib import Path

path = Path(sys.argv[1]) / "models/glm5next/nvidia/ops/kpool_compress.py"
source = path.read_text()
MARK = "kpool-seed-stride"
edits = [
    ("""    tail_ptr,
    n_tokens,
    HEAD_DIM: tl.constexpr,
    KPOOL: tl.constexpr,
    BLOCK_D: tl.constexpr,
):""",
     """    tail_ptr,
    n_tokens,
    TAIL_BLOCK_ELEMS: tl.constexpr,
    KPOOL_HEAD: tl.constexpr,
    HEAD_DIM: tl.constexpr,
    KPOOL: tl.constexpr,
    BLOCK_D: tl.constexpr,
):"""),
    ("""    ``tail[block, {0:K, 1:score}, pos % KPOOL, :]``.
    \"\"\"
    i = tl.program_id(0)""",
     """    ``tail[block, {0:K, 1:score}, pos % KPOOL, :]``.

    The tail cache aliases the indexer cache with the indexer's (padded) block
    stride, so blocks are addressed through ``TAIL_BLOCK_ELEMS`` /
    ``KPOOL_HEAD`` (``tail.stride(0)`` / ``tail.stride(1)``), never as a dense
    ``[num_blocks, 2, KPOOL, HEAD_DIM]`` array (kpool-seed-stride).
    \"\"\"
    i = tl.program_id(0)"""),
    ("    base = (blk * 2 * KPOOL + t % KPOOL) * HEAD_DIM\n",
     "    base = blk * TAIL_BLOCK_ELEMS + (t % KPOOL) * HEAD_DIM\n"),
    ("    tl.store(tail_ptr + base + KPOOL * HEAD_DIM + offs, s, mask=m)\n",
     "    tl.store(tail_ptr + base + KPOOL_HEAD + offs, s, mask=m)\n"),
    ("""    \"\"\"Seed the paged tail cache from a prefill batch (see the kernel).\"\"\"
    assert tail_kv_cache.dtype == torch.bfloat16
""",
     """    \"\"\"Seed the paged tail cache from a prefill batch (see the kernel).\"\"\"
    assert tail_kv_cache.dtype == torch.bfloat16
    assert tail_kv_cache.ndim == 4 and tail_kv_cache.shape[1] == 2
    assert tail_kv_cache.stride(3) == 1 and tail_kv_cache.stride(2) == head_dim
"""),
    ("""        tail_kv_cache,
        n,
        HEAD_DIM=head_dim,
        KPOOL=kpool,""",
     """        tail_kv_cache,
        n,
        TAIL_BLOCK_ELEMS=tail_kv_cache.stride(0),
        KPOOL_HEAD=tail_kv_cache.stride(1),
        HEAD_DIM=head_dim,
        KPOOL=kpool,"""),
]
if MARK not in source:
    for old, new in edits:
        if source.count(old) != 1:
            raise RuntimeError(f"kpool seed kernel source drift ({source.count(old)}):\n{old}")
        source = source.replace(old, new)
    compile(source, str(path), "exec")
    path.write_text(source)

print("GLM kpool prefill seed addresses tail blocks by the padded stride (vLLM #57477)")
