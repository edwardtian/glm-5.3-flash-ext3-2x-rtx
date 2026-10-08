#!/usr/bin/env python3
"""GPU regression gate for the actual kpool expansion and DCP1 masking pipeline.

Run inside the recipe image. --expect-unpatched reproduces the v0.9.0 loss;
the default checks preservation, stable order, strided buffers and graph replay.
"""

import argparse
import json
from pathlib import Path

import torch
from vllm.models.glm5next.nvidia.ops.kpool_compress import (
    expand_pools_and_append_tail,
)
from vllm.v1.attention.backends.mla import b12x_mla_sparse as backend


LENGTHS = [1, 2, 3, 4, 5, 1001, 1002, 1003, 1004,
           2041, 2042, 2043, 2044, 2045, 2046, 2047, 2048, 2050]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-unpatched", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    device = "cuda"
    lens = torch.tensor(LENGTHS, dtype=torch.int32, device=device)
    pools = torch.arange(511, dtype=torch.int32, device=device).repeat(len(LENGTHS), 1)
    pools[pools >= (lens // 4)[:, None]] = -1
    logical = expand_pools_and_append_tail(pools, lens, 4)
    width = logical.shape[1]
    # Non-identity block placement exercises the same logical-to-physical
    # conversion used by forward_mqa, before the compaction and mask.
    blocks_per_req = 40
    block_table = torch.arange(len(LENGTHS) * blocks_per_req, device=device,
                               dtype=torch.int32).reshape(len(LENGTHS), blocks_per_req).flip(1)
    req_ids = torch.arange(len(LENGTHS), dtype=torch.int32, device=device)
    original = torch.empty_like(logical)
    backend._logical_topk_to_physical_slots(req_ids, block_table, logical, original, 64)
    initial_lens = lens.clamp_max(width)
    dropped_rows = ((original >= 0) &
                    (torch.arange(width, device=device)[None, :] >= initial_lens[:, None])).any(1)
    expected = []
    for row, length in enumerate(LENGTHS):
        positions = torch.cat((torch.arange(min(length // 4, 511) * 4, device=device),
                               torch.arange(length // 4 * 4, length, device=device)))
        expected.append(block_table[row, positions // 64] * 64 + positions % 64)

    reports = []
    modes = ["baseline"] if args.expect_unpatched else ["contiguous", "strided", "cuda-graph"]
    for mode in modes:
        storage = torch.full((len(LENGTHS) * 2, width * 2), -777,
                             dtype=torch.int32, device=device)
        table = storage[::2, ::2] if mode == "strided" else original.clone()
        table.copy_(original)
        nsa_lens = initial_lens.clone()
        if not args.expect_unpatched:
            backend._compact_dropped_selection(table, nsa_lens)
            assert torch.equal(table[~dropped_rows], original[~dropped_rows])
            assert torch.equal(nsa_lens[~dropped_rows], initial_lens[~dropped_rows])
            for row in torch.where(dropped_rows)[0].tolist():
                selected = original[row][original[row] >= 0]
                assert torch.equal(table[row, :selected.numel()], selected), "selection order changed"
                assert int(nsa_lens[row]) == selected.numel()
            if mode == "cuda-graph":
                graph = torch.cuda.CUDAGraph()
                with torch.cuda.graph(graph):
                    backend._compact_dropped_selection(table, nsa_lens)
                    backend._mask_page_table_after_nsa_len(table, nsa_lens)
                table.copy_(original)
                nsa_lens.copy_(initial_lens)
                graph.replay()
            else:
                backend._mask_page_table_after_nsa_len(table, nsa_lens)
        else:
            backend._mask_page_table_after_nsa_len(table, nsa_lens)
        rows = []
        for row, length in enumerate(LENGTHS):
            attended = table[row][table[row] >= 0]
            missing = expected[row].numel() - attended.numel()
            if args.expect_unpatched and bool(dropped_rows[row]):
                assert missing == length % 4 and missing > 0
            else:
                assert torch.equal(attended.sort().values, expected[row].sort().values), (mode, length)
            rows.append({"context_length": length, "missing_tokens": missing})
        if mode == "strided":
            assert bool((storage[1::2] == -777).all())
            assert bool((storage[::2, 1::2] == -777).all())
        reports.append({"mode": mode, "rows": rows})

    # Dense short-prefill rows already have a contiguous causal selection.
    if not args.expect_unpatched:
        dense = torch.full_like(original, -1)
        for row, selected in enumerate(expected):
            dense[row, :selected.numel()] = selected
        before = dense.clone()
        nsa_lens = initial_lens.clone()
        backend._compact_dropped_selection(dense, nsa_lens)
        assert torch.equal(dense, before)
        assert torch.equal(nsa_lens, initial_lens)
        for shape in ((0, width), (1, 0)):
            backend._compact_dropped_selection(torch.empty(shape, dtype=torch.int32, device=device),
                                               torch.zeros(shape[0], dtype=torch.int32, device=device))

    receipt = {"schema": "glm53-dcp1-kpool-tail.v1", "passed": True,
               "expect_unpatched": args.expect_unpatched, "gpu": torch.cuda.get_device_name(),
               "checks": reports, "dense_rows_unchanged": not args.expect_unpatched}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
