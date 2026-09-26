#!/usr/bin/env python3
"""Place GLM-5.3 MLA attention layers on one owner GPU instead of DCP2.

Each owned MLA layer (projections, sparse indexer, latent KV and indexer
cache) exists only on its owner rank and runs there unsharded with all 64
heads. The other rank holds no weights or cache for that layer and feeds
zeros into the decoder's existing post-attention TP all-reduce, so both ranks
leave the layer with the owner's output and the MoE/EP2 half of the layer is
unchanged. KDA layers stay TP2: their recurrent state is per head, so head
sharding splits both weights and state without replication or extra traffic.

``VLLM_GLM53_MLA_OWNERS`` selects the placement:
  ``tp``                 legacy TP (+ optional DCP) attention, no ownership
  ``split:N``            MLA layers < N on rank 0, >= N on rank 1
  ``map:3=0,7=1,...``    explicit per-layer owner ranks

The KV allocator projects GLM's hybrid groups onto each rank's local layers
(the pipeline-parallel projection path). Mamba (KDA) groups share MLA slot
tensors, so the number of mamba groups must fit the rank that owns the fewest
MLA layers.
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

# --------------------------------------------------------------------------
# Shared owner-map resolution (importable from the engine core and workers).
# --------------------------------------------------------------------------
placement = root / "models/glm5next/nvidia/placement.py"
placement_source = '''# SPDX-License-Identifier: Apache-2.0
"""GLM-5.3 MLA layer ownership (recipe patch; see port-glm53-layer-owner.py)."""

from __future__ import annotations

import functools
import json
import os

import torch

from vllm.logger import init_logger

logger = init_logger(__name__)

ENV = "VLLM_GLM53_MLA_OWNERS"


@functools.cache
def _parse(spec: str, mla_layers: tuple[int, ...], world: int) -> dict[int, int]:
    spec = spec.strip()
    if spec in {"", "tp", "off", "none"}:
        return {}
    if spec.startswith("split:"):
        boundary = int(spec.split(":", 1)[1])
        if world != 2:
            raise ValueError(f"{ENV}=split:N requires TP=2, got TP={world}")
        owners = {layer: int(layer >= boundary) for layer in mla_layers}
    elif spec.startswith("map:"):
        owners = {}
        for item in spec.split(":", 1)[1].split(","):
            layer, rank = item.split("=")
            owners[int(layer)] = int(rank)
        missing = sorted(set(mla_layers) - set(owners))
        extra = sorted(set(owners) - set(mla_layers))
        if missing or extra:
            raise ValueError(
                f"{ENV} map must name every MLA layer exactly: "
                f"missing={missing} extra={extra}"
            )
    else:
        raise ValueError(f"{ENV} must be tp, split:N or map:L=R,...; got {spec!r}")
    bad = {layer: rank for layer, rank in owners.items() if not 0 <= rank < world}
    if bad:
        raise ValueError(f"{ENV} owner ranks out of range for TP={world}: {bad}")
    counts = [sum(rank == r for rank in owners.values()) for r in range(world)]
    if min(counts) == 0:
        raise ValueError(
            f"{ENV} leaves a rank without MLA layers {counts}; GLM's KDA state "
            "shares MLA slot tensors, so every rank needs at least one"
        )
    return owners


def _mla_layers(hf_text_config) -> tuple[int, ...]:
    return tuple(
        i
        for i in range(hf_text_config.num_hidden_layers)
        if not hf_text_config.is_kda_layer(i)
    )


def mla_owner_map(vllm_config) -> dict[int, int]:
    """Return {mla_layer: owner_rank}; empty when ownership is disabled."""
    spec = os.environ.get(ENV, "tp")
    hf_text_config = vllm_config.model_config.hf_text_config
    if getattr(hf_text_config, "model_type", None) not in {
        "glm5_next",
        "glm5_next_text",
    }:
        return {}
    parallel = vllm_config.parallel_config
    owners = _parse(
        spec, _mla_layers(hf_text_config), int(parallel.tensor_parallel_size)
    )
    if owners and int(parallel.decode_context_parallel_size) != 1:
        raise ValueError(f"{ENV}={spec} replaces DCP; set DCP=1")
    if owners and int(parallel.pipeline_parallel_size) != 1:
        raise ValueError(f"{ENV}={spec} requires PP=1")
    return owners


def mla_owner(layer_idx: int, vllm_config) -> int | None:
    return mla_owner_map(vllm_config).get(layer_idx)


def mla_counts_per_rank(vllm_config) -> list[int] | None:
    owners = mla_owner_map(vllm_config)
    if not owners:
        return None
    world = int(vllm_config.parallel_config.tensor_parallel_size)
    return [sum(rank == r for rank in owners.values()) for r in range(world)]


class Glm5NextRemoteAttention(torch.nn.Module):
    """Placeholder for an MLA layer owned by the other TP rank.

    Holds no weights and registers no KV cache. The weight loader treats its
    prefix as missing (see ``is_pp_missing_parameter``) so the owner-only
    checkpoint tensors are skipped on this rank.
    """

    glm53_owner_mode = "remote"

    def forward(self, hidden_states: torch.Tensor, positions: torch.Tensor):
        return torch.zeros_like(hidden_states)


def attention_forward(attn, hidden_states: torch.Tensor, positions: torch.Tensor):
    """Run attention; complete owned layers with the TP all-reduce."""
    mode = getattr(attn, "glm53_owner_mode", None)
    if mode is None:
        return attn(hidden_states=hidden_states, positions=positions)
    from vllm.distributed import tensor_model_parallel_all_reduce

    out = attn(hidden_states=hidden_states, positions=positions)
    return tensor_model_parallel_all_reduce(out)


def build_mla_attention(cls, **kwargs):
    """Construct an owned (unsharded) MLA layer or its remote placeholder."""
    from vllm.distributed import get_tensor_model_parallel_rank
    from vllm.model_executor.models.utils import extract_layer_index

    vllm_config = kwargs["vllm_config"]
    layer_idx = extract_layer_index(kwargs["prefix"])
    owner = None
    if layer_idx < vllm_config.model_config.hf_text_config.num_hidden_layers:
        owner = mla_owner(layer_idx, vllm_config)
    if owner is None:
        return cls(**kwargs)
    if get_tensor_model_parallel_rank() != owner:
        return Glm5NextRemoteAttention()
    module = cls(owner_local=True, **kwargs)
    module.glm53_owner_mode = "local"
    return module


_COMPONENTS = (
    ("vision", ("visual.",)),
    ("embed", ("embed_tokens",)),
    ("lm_head", ("lm_head",)),
    ("routed_experts", (".mlp.experts.",)),
    ("shared_experts", ("shared_expert",)),
    ("router", (".mlp.gate.",)),
)


def _classify(name: str, mla: set[int], kda: set[int]) -> str:
    for component, needles in _COMPONENTS:
        if any(needle in name for needle in needles):
            return component
    marker = ".layers."
    if marker in name:
        layer = int(name.split(marker, 1)[1].split(".", 1)[0])
        if ".self_attn." in name:
            return "mla_attention" if layer in mla else "kda_attention"
        if ".mlp." in name:
            return "dense_mlp"
        return "layer_other"
    return "other"


def log_placement_ledger(model, vllm_config, extra_models=()) -> None:
    """Log a per-rank byte ledger of resident parameters and buffers."""
    try:
        from vllm.distributed import get_tensor_model_parallel_rank

        hf_text_config = vllm_config.model_config.hf_text_config
        mla = set(_mla_layers(hf_text_config))
        kda = set(range(hf_text_config.num_hidden_layers)) - mla
        ledger: dict[str, int] = {}
        seen: set[int] = set()
        sources = [("", model)] + [(f"{tag}.", m) for tag, m in extra_models]
        for tag, module in sources:
            if module is None:
                continue
            tensors = list(module.named_parameters()) + list(module.named_buffers())
            for name, tensor in tensors:
                if tensor.device.type != "cuda":
                    continue
                storage = tensor.untyped_storage()
                key = storage.data_ptr()
                if key in seen:
                    continue
                seen.add(key)
                component = "draft" if tag else _classify(name, mla, kda)
                ledger[component] = ledger.get(component, 0) + storage.nbytes()
        owners = mla_owner_map(vllm_config)
        rank = get_tensor_model_parallel_rank()
        record = {
            "rank": rank,
            "mla_owned": sorted(k for k, v in owners.items() if v == rank),
            "gib": {k: round(v / 2**30, 3) for k, v in sorted(ledger.items())},
            "total_gib": round(sum(ledger.values()) / 2**30, 3),
            "torch_allocated_gib": round(torch.cuda.memory_allocated() / 2**30, 3),
        }
        logger.info("glm53-placement ledger %s", json.dumps(record, sort_keys=True))
    except Exception as exc:  # diagnostics must never break serving
        logger.warning("glm53-placement ledger unavailable: %s", exc)
'''
if not placement.exists() or placement.read_text(encoding="utf-8") != placement_source:
    placement.write_text(placement_source, encoding="utf-8")
compile(placement_source, str(placement), "exec")

# --------------------------------------------------------------------------
# Owner-local MLA construction: no TP sharding, no internal reduction.
# --------------------------------------------------------------------------
attention = root / "models/glm5next/nvidia/attention.py"
replace_once(
    attention,
    """        input_size: int | None = None,
        skip_rope: bool | None = False,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size""",
    """        input_size: int | None = None,
        skip_rope: bool | None = False,
        owner_local: bool = False,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        # GLM53 layer ownership: the owner runs every head unsharded; the
        # decoder completes the output with the TP all-reduce.
        self.owner_local = bool(owner_local)""",
    "MLA signature",
)
replace_once(
    attention,
    """        self.num_heads = num_heads
        tp_size = get_tensor_model_parallel_world_size()
        assert num_heads % tp_size == 0""",
    """        self.num_heads = num_heads
        tp_size = 1 if self.owner_local else get_tensor_model_parallel_world_size()
        assert num_heads % tp_size == 0""",
    "MLA TP size",
)
replace_once(
    attention,
    """                quant_config=quant_config,
                prefix=f"{prefix}.q_b_proj",
            )""",
    """                quant_config=quant_config,
                prefix=f"{prefix}.q_b_proj",
                disable_tp=self.owner_local,
            )""",
    "MLA q_b_proj",
)
replace_once(
    attention,
    """            quant_config=quant_config,
            prefix=f"{prefix}.kv_b_proj",
        )""",
    """            quant_config=quant_config,
            prefix=f"{prefix}.kv_b_proj",
            disable_tp=self.owner_local,
        )""",
    "MLA kv_b_proj",
)
replace_once(
    attention,
    """            quant_config=quant_config,
            prefix=f"{prefix}.o_proj",
        )

        if not skip_rope:""",
    """            quant_config=quant_config,
            prefix=f"{prefix}.o_proj",
            disable_tp=self.owner_local,
            reduce_results=not self.owner_local,
        )

        if not skip_rope:""",
    "MLA o_proj",
)

# --------------------------------------------------------------------------
# Decoder: build owned layers through the placement helper; reduce owned
# outputs across TP.
# --------------------------------------------------------------------------
model = root / "models/glm5next/nvidia/model.py"
replace_once(
    model,
    """            self.self_attn = Glm5NextMLAAttention(
                vllm_config=vllm_config,""",
    """            self.self_attn = glm53_build_mla_attention(
                Glm5NextMLAAttention,
                vllm_config=vllm_config,""",
    "decoder MLA construction",
)
replace_once(
    model,
    """        x = self.self_attn(
            hidden_states=x,
            positions=positions,
        )

        if self.is_sequence_parallel:
            x = sp_reduce_scatter(x)""",
    """        x = glm53_attention_forward(self.self_attn, x, positions)

        if self.is_sequence_parallel:
            x = sp_reduce_scatter(x)""",
    "decoder mHC attention call",
)
replace_once(
    model,
    """from vllm.model_executor.models.utils import (
    AutoWeightsLoader,
    PPMissingLayer,""",
    """from vllm.models.glm5next.nvidia.placement import (
    attention_forward as glm53_attention_forward,
)
from vllm.models.glm5next.nvidia.placement import (
    build_mla_attention as glm53_build_mla_attention,
)
from vllm.model_executor.models.utils import (
    AutoWeightsLoader,
    PPMissingLayer,""",
    "placement import",
)

# The placeholder must look like a pipeline-missing layer so the loader skips
# the owner's checkpoint tensors on the other rank.
utils = root / "model_executor/models/utils.py"
replace_once(
    utils,
    """    if isinstance(model, (StageMissingLayer, PPMissingLayer)):
        return True

    return any(""",
    """    if isinstance(model, (StageMissingLayer, PPMissingLayer)):
        return True
    if getattr(model, "glm53_owner_mode", None) == "remote":
        return True

    return any(""",
    "missing-parameter owner check",
)
utils_source = utils.read_text(encoding="utf-8")
missing_names_anchor = "def get_pp_missing_layer_names(model: torch.nn.Module) -> list[str]:"
if missing_names_anchor not in utils_source:
    raise RuntimeError(f"{utils}: get_pp_missing_layer_names anchor drift")
start = utils_source.index(missing_names_anchor)
body_end = utils_source.index("\ndef ", start + 1)
body = utils_source[start:body_end]
old_check = "if isinstance(module, (StageMissingLayer, PPMissingLayer)):"
new_check = (
    "if isinstance(module, (StageMissingLayer, PPMissingLayer)) or (\n"
    '            getattr(module, "glm53_owner_mode", None) == "remote"\n'
    "        ):"
)
if new_check not in body:
    if body.count(old_check) != 1:
        raise RuntimeError(f"{utils}: missing-layer scan anchor drift")
    utils_source = (
        utils_source[:start]
        + body.replace(old_check, new_check)
        + utils_source[body_end:]
    )
    compile(utils_source, str(utils), "exec")
    utils.write_text(utils_source, encoding="utf-8")

# --------------------------------------------------------------------------
# KV groups: every rank's projected mamba group must fit its local MLA slots.
# --------------------------------------------------------------------------
kv_utils = root / "v1/core/kv_cache_utils.py"
replace_once(
    kv_utils,
    """    num_groups = cdiv(len(mamba_layer_names), len(mla_layer_names))
    pp_size = vllm_config.parallel_config.pipeline_parallel_size
    if pp_size == 1:
        return num_groups
""",
    """    num_groups = cdiv(len(mamba_layer_names), len(mla_layer_names))
    pp_size = vllm_config.parallel_config.pipeline_parallel_size
    from vllm.models.glm5next.nvidia.placement import mla_counts_per_rank

    owned_counts = mla_counts_per_rank(vllm_config)
    if owned_counts is not None:
        # Layer ownership: KDA layers are TP-sharded onto every rank while
        # each rank holds only its owned MLA slots.
        return max(
            num_groups,
            *(cdiv(len(mamba_layer_names), count) for count in owned_counts),
        )
    if pp_size == 1:
        return num_groups
""",
    "mamba group count",
)

replace_once(
    kv_utils,
    """    num_tokens, max_concurrency = get_kv_cache_capacity(vllm_config, kv_cache_config)
    vllm_config.cache_config.kv_cache_size_tokens = num_tokens""",
    """    num_tokens, max_concurrency = get_kv_cache_capacity(vllm_config, kv_cache_config)
    try:
        import json as _json

        _groups = []
        for _group in kv_cache_config.kv_cache_groups:
            _spec = _group.kv_cache_spec
            _groups.append(
                {
                    "spec": type(_spec).__name__,
                    "layers": len(_group.layer_names),
                    "block_size": getattr(_spec, "block_size", None),
                    "page_bytes": _spec.page_size_bytes,
                    "blocks_per_max_request": cdiv(
                        _spec.max_memory_usage_bytes(vllm_config),
                        _spec.page_size_bytes,
                    ),
                }
            )
        logger.info(
            "glm53-placement kv-capacity %s",
            _json.dumps(
                {
                    "num_blocks": kv_cache_config.num_blocks,
                    "tensor_bytes": sum(t.size for t in kv_cache_config.kv_cache_tensors),
                    "groups": _groups,
                }
            ),
        )
    except Exception as _exc:  # diagnostics must never break serving
        logger.warning("glm53-placement kv-capacity unavailable: %s", _exc)
    vllm_config.cache_config.kv_cache_size_tokens = num_tokens""",
    "kv capacity breakdown",
)

# --------------------------------------------------------------------------
# Placement ledger after model (and drafter) load.
# --------------------------------------------------------------------------
runner = root / "v1/worker/gpu/model_runner.py"
replace_once(
    runner,
    """        self.model_memory_usage = m.consumed_memory
        logger.info(
            "Model loading took %s GiB and %.6f seconds",
            format_gib(m.consumed_memory),
            time_after_load - time_before_load,
        )
""",
    """        self.model_memory_usage = m.consumed_memory
        logger.info(
            "Model loading took %s GiB and %.6f seconds",
            format_gib(m.consumed_memory),
            time_after_load - time_before_load,
        )
        from vllm.models.glm5next.nvidia.placement import log_placement_ledger

        _draft = getattr(getattr(self, "speculator", None), "model", None)
        if _draft is None:
            _draft = getattr(
                getattr(getattr(self, "speculator", None), "drafter", None),
                "model",
                None,
            )
        log_placement_ledger(
            self.model,
            self.vllm_config,
            extra_models=(("draft", _draft),) if _draft is not None else (),
        )
""",
    "placement ledger",
)
print("GLM-5.3 MLA layer ownership patch applied")
