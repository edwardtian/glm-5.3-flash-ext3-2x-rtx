#!/usr/bin/env python3
"""Store the DFlash draft window inside GLM-5.3's shared MLA slot tensors.

All GLM-5.3 cache groups draw block IDs from one vLLM BlockPool, and every
block ID owns one page in each MLA and indexer slot tensor. The recipe's
earlier layout gave the five DFlash sliding-window layers separate tensors
with 128-token pages, so a 2K window plus a prefill chunk consumed 49 block
IDs per request and every one of them stranded the MLA/indexer pages of that
block (about 790 MiB per request per GPU on the K4 profile).

With ``VLLM_GLM53_DRAFT_SLOT_SHARING=1`` (DCP=1 only), each draft layer
instead co-owns one MLA slot tensor, exactly like KDA state does. The draft
block size becomes the largest multiple of 16 that divides the MLA block size
and whose natural page fits the MLA page, so the scheduler's LCM block size
is unchanged and FlashAttention uses the allocator block directly. Draft
pages are padded to the MLA page; FlashAttention's blocks-first layout already
receives a strided view that skips the padding.
"""

import sys
from pathlib import Path


def replace_once(path: Path, old: str, new: str, description: str) -> None:
    source = path.read_text(encoding="utf-8")
    if new in source:
        return
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one {description} anchor, found {count}")
    source = source.replace(old, new)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")


root = Path(sys.argv[1])
kv_utils = root / "v1/core/kv_cache_utils.py"

# 1. Resize and pad draft specs when grouping.
replace_once(
    kv_utils,
    """    uniform_spec = UniformTypeKVCacheSpecs.from_specs(mla_specs)
    assert uniform_spec is not None
    draft_group = None
    if draft_specs:""",
    """    uniform_spec = UniformTypeKVCacheSpecs.from_specs(mla_specs)
    assert uniform_spec is not None
    draft_specs = _glm53_slot_shared_draft_specs(
        vllm_config, draft_specs, mla_specs, mla_names, mla_page
    )
    draft_group = None
    if draft_specs:""",
    "draft spec resize",
)
replace_once(
    kv_utils,
    """def _get_kv_cache_groups_glm5_next(""",
    """def _glm53_slot_shared_draft_specs(
    vllm_config: VllmConfig,
    draft_specs: dict[str, KVCacheSpec],
    mla_specs: dict[str, KVCacheSpec],
    mla_names: list[str],
    mla_page: int,
) -> dict[str, KVCacheSpec]:
    \"\"\"Resize DFlash draft pages so each draft layer co-owns one MLA slot.\"\"\"
    import os

    if os.environ.get("VLLM_GLM53_DRAFT_SLOT_SHARING", "0") in ("", "0"):
        return draft_specs
    if not draft_specs:
        return draft_specs
    if vllm_config.parallel_config.decode_context_parallel_size != 1:
        logger.warning("glm53 draft slot sharing requires DCP=1; keeping 128-token draft pages")
        return draft_specs
    from vllm.models.glm5next.nvidia.placement import mla_counts_per_rank

    counts = mla_counts_per_rank(vllm_config) or [len(mla_names)]
    if len(draft_specs) > min(counts):
        logger.warning(
            "glm53 draft slot sharing needs %d MLA slots per rank; ranks have %s",
            len(draft_specs),
            counts,
        )
        return draft_specs
    mla_block = next(iter(mla_specs.values())).block_size
    resized: dict[str, KVCacheSpec] = {}
    for name, spec in draft_specs.items():
        if not isinstance(spec, AttentionSpec) or spec.block_size <= 0:
            return draft_specs
        per_token = spec.real_page_size_bytes // spec.block_size
        candidates = [
            size
            for size in range(16, mla_block + 1, 16)
            if mla_block % size == 0 and size * per_token <= mla_page
        ]
        if not candidates:
            return draft_specs
        block = max(candidates)
        resized[name] = replace(spec, block_size=block, page_size_padded=mla_page)
    blocks = {s.block_size for s in resized.values()}
    logger.info(
        "glm53 draft slot sharing: %d draft layers in MLA slots, block_size=%s, "
        "page=%d of %d bytes",
        len(resized),
        sorted(blocks),
        next(iter(resized.values())).real_page_size_bytes,
        mla_page,
    )
    return resized


def _glm53_is_slot_shared(group: KVCacheGroupSpec, mla_page: int) -> bool:
    spec = group.kv_cache_spec
    inner = (
        spec.kv_cache_specs.values()
        if isinstance(spec, UniformTypeKVCacheSpecs)
        else [spec]
    )
    return all(getattr(s, "page_size_padded", None) == mla_page for s in inner)


def _get_kv_cache_groups_glm5_next(""",
    "draft slot helper",
)

# 2. Pool bytes per block excludes slot-shared draft pages.
replace_once(
    kv_utils,
    """        _, _, mla_names, idx_names, mla_page, idx_page, _, _, draft_groups = glm5
        return (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(g.kv_cache_spec.page_size_bytes for g in draft_groups)
        )""",
    """        _, _, mla_names, idx_names, mla_page, idx_page, _, _, draft_groups = glm5
        return (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(
                g.kv_cache_spec.page_size_bytes
                for g in draft_groups
                if not _glm53_is_slot_shared(g, mla_page)
            )
        )""",
    "pool bytes per block",
)

# 3. Tensor emission: draft layers co-own MLA slot tensors.
replace_once(
    kv_utils,
    """        per_block = (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(g.kv_cache_spec.page_size_bytes for g in draft_groups)
        )
        num_blocks = available_memory // per_block""",
    """        shared_drafts = [g for g in draft_groups if _glm53_is_slot_shared(g, mla_page)]
        separate_drafts = [g for g in draft_groups if g not in shared_drafts]
        for _group in shared_drafts:
            assert len(_group.layer_names) <= len(mla_names), (
                "slot-shared draft layers exceed this rank's MLA slots"
            )
        per_block = (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(g.kv_cache_spec.page_size_bytes for g in separate_drafts)
        )
        num_blocks = available_memory // per_block""",
    "tensor per-block bytes",
)
replace_once(
    kv_utils,
    """                shared_by=[mla_name]
                + [g.layer_names[i] for g in mamba_groups if i < len(g.layer_names)],
            )
            for i, mla_name in enumerate(mla_names)""",
    """                shared_by=[mla_name]
                + [g.layer_names[i] for g in mamba_groups if i < len(g.layer_names)]
                + [g.layer_names[i] for g in shared_drafts if i < len(g.layer_names)],
            )
            for i, mla_name in enumerate(mla_names)""",
    "MLA slot sharing",
)
replace_once(
    kv_utils,
    """            for group in draft_groups
            for group_spec in [cast(UniformTypeKVCacheSpecs, group.kv_cache_spec)]
            for layer_name in group.layer_names
        ]""",
    """            for group in separate_drafts
            for group_spec in [cast(UniformTypeKVCacheSpecs, group.kv_cache_spec)]
            for layer_name in group.layer_names
        ]""",
    "separate draft tensors",
)

replace_once(
    kv_utils,
    """        per_block = (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(g.kv_cache_spec.page_size_bytes for g in draft_groups)
        )
        return blocks_needed * per_block""",
    """        per_block = (
            len(mla_names) * mla_page
            + len(idx_names) * idx_page
            + sum(
                g.kv_cache_spec.page_size_bytes
                for g in draft_groups
                if not _glm53_is_slot_shared(g, mla_page)
            )
        )
        return blocks_needed * per_block""",
    "max-request memory bytes",
)
print("GLM-5.3 draft slot-sharing patch applied")
