#!/usr/bin/env python3
"""Keep the kpool tail in DCP1 sparse-MLA selections below ~2044 tokens of context.

The bug, in tpurtell v0.9.0 (and v0.8.0, same file):
  The GLM-5.3 kpool indexer writes 511 selected pools (columns 0..2043) and then the request's
  incomplete trailing pool, the "tail" (0-3 tokens, including the newest token), at the FIXED
  columns 2044..2046 (kpool_compress.expand_pools_and_append_tail).
  In the DCP1 branch of B12xMLASparseImpl.forward_mqa, nsa_cache_seqlens becomes
  min(causal length, 2048), and _mask_page_table_after_nsa_len then writes -1 at every column
  >= that length.
  For a causal length <= 2043 that is not a multiple of 4, the tail columns sit past the length,
  so they are erased. The newest 1-3 tokens are then invisible to all 11 MLA layers.
  The same happens to the first ~2043 rows of a sparse-path prefill: a batch whose longest
  prefill exceeds 2048 tokens.
  The DCP2 branch compacts the winners (triton_filter_and_convert_dcp_index) and is not affected.

The fix: one Triton kernel, run just before the existing positional mask in the DCP1 logical-id
branch only. For each row it checks whether any valid entry (>= 0) lies at or past nsa_len,
i.e. whether the stock mask is about to drop a selected token. Only for such rows, it
stable-compacts the valid entries to the front and sets nsa_len to their count.
  - Rows the stock code handles correctly are not written at all. Byte-identical:
      * the dense short-prefill path (entries are 0..pos at columns 0..pos);
      * every row with causal length >= 2045;
      * every row whose length is a multiple of 4.
  - The DCP2 branch, the ckv-gather branch and the physical-slot branch are untouched.
  - A fixed row attends exactly the set the indexer selected: the history pools plus the tail.
    Up to a causal length of 2047 that is every token 0..len-1, the same set the dense
    short-prefill path attends at that position. History order is preserved (stable), and the
    kernel sees the same input shape the DCP2 and short-prefill paths already produce (a
    contiguous valid prefix with nsa_len = count).

In-place safety: the whole row is loaded into registers before any store. The cumsum and count
reductions are block-wide and synchronise all warps after the loads. The two stores hit
disjoint addresses: valid values go to [0, count), and -1 goes to [count, width).

Atomic: every edit is validated (target sha256 + one occurrence of each anchor) and the
result compiled before anything is written; the file is replaced with os.replace.
Usage: port-dcp1-kpool-tail-glm53.py VLLM_DIR
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

MARK = "dcp1-kpool-tail"
REL = "v1/attention/backends/mla/b12x_mla_sparse.py"
# tpurtell v0.9.0 (f36dfb87) and v0.8.0 (e4d37a01) ship this exact file.
EXPECTED_SHA256 = "d3b1c6704033797613b9767fc07a57a84f6b042c54d0a4862155ec52ecb99260"

path = Path(sys.argv[1]) / REL
raw = path.read_bytes()
source = raw.decode()

if MARK in source:
    print(f"{MARK}: already applied to {path}")
    sys.exit(0)

digest = hashlib.sha256(raw).hexdigest()
if digest != EXPECTED_SHA256:
    raise SystemExit(f"{MARK}: refusing, unexpected source {path} sha256 {digest} (expected {EXPECTED_SHA256})")

KERNEL = '''

@triton.jit
def _compact_dropped_selection_kernel(  # dcp1-kpool-tail
    page_table_ptr,
    nsa_len_ptr,
    page_stride0,
    page_stride1,
    width: tl.constexpr,
    BLOCK_W: tl.constexpr,
):
    row = tl.program_id(0)
    offs = tl.arange(0, BLOCK_W)
    in_row = offs < width
    ptrs = page_table_ptr + row * page_stride0 + offs * page_stride1
    vals = tl.load(ptrs, mask=in_row, other=-1)
    nsa_len = tl.load(nsa_len_ptr + row)
    valid = in_row & (vals >= 0)
    dropped = tl.sum((valid & (offs >= nsa_len)).to(tl.int32), axis=0)
    if dropped > 0:
        valid_i = valid.to(tl.int32)
        dest = tl.cumsum(valid_i, axis=0) - 1
        count = tl.sum(valid_i, axis=0)
        tl.store(page_table_ptr + row * page_stride0 + dest * page_stride1, vals, mask=valid)
        tl.store(ptrs, -1, mask=in_row & (offs >= count))
        tl.store(nsa_len_ptr + row, count)


def _compact_dropped_selection(  # dcp1-kpool-tail
    page_table: torch.Tensor,
    nsa_cache_seqlens: torch.Tensor,
) -> None:
    """Stable-compact a row's valid ids to the front, and set its length to their
    count, ONLY when the positional mask would otherwise drop a selected id (the
    kpool tail at fixed columns 2044..2046 below ~2044 tokens). Other rows are not
    written."""
    width = page_table.shape[1]
    if width == 0 or page_table.shape[0] == 0:
        return
    _compact_dropped_selection_kernel[(page_table.shape[0],)](
        page_table,
        nsa_cache_seqlens,
        page_table.stride(0),
        page_table.stride(1),
        width,
        BLOCK_W=triton.next_power_of_2(width),
        num_warps=8,
    )
'''

edits = [
    # 1. Add the kernel right after the stock positional-mask helper.
    (
        """        page_table.stride(1),
        width,
        BLOCK_N=block_n,
    )
""",
        """        page_table.stride(1),
        width,
        BLOCK_N=block_n,
    )
""" + KERNEL,
    ),
    # 2. DCP1 logical-id branch only: compact before the positional mask.
    (
        """                nsa_cache_seqlens.copy_(per_token_cache)
                nsa_cache_seqlens.clamp_max_(topk_indices.shape[1])
                _mask_page_table_after_nsa_len(
                    selected_indices, nsa_cache_seqlens
                )
""",
        """                nsa_cache_seqlens.copy_(per_token_cache)
                nsa_cache_seqlens.clamp_max_(topk_indices.shape[1])
                # dcp1-kpool-tail: keep the kpool tail (fixed columns
                # 2044..2046) when the causal length is below the column.
                _compact_dropped_selection(
                    selected_indices, nsa_cache_seqlens
                )
                _mask_page_table_after_nsa_len(
                    selected_indices, nsa_cache_seqlens
                )
""",
    ),
]

for old, _ in edits:
    n = source.count(old)
    if n != 1:
        raise SystemExit(f"{MARK}: refusing, anchor found {n} times (expected 1):\n{old}")
for old, new in edits:
    source = source.replace(old, new)
compile(source, str(path), "exec")

fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dcp1-kpool-tail-")
try:
    with os.fdopen(fd, "w") as fh:
        fh.write(source)
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise

print(f"{MARK}: DCP1 sparse-MLA selections keep the kpool tail below 2044 tokens ({path})")
