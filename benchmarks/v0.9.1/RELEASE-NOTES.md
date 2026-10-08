# v0.9.1 — keep the newest tokens in sparse attention

Thanks to **Michael M. (@UrbanAstroLA)** for the deep investigation, carefully scoped fix, and extensive GPU and serving evidence in [PR #6](https://github.com/tpurtell/glm-5.3-flash-ext3-2x-rtx/pull/6).

## Fix

DCP1 sparse-MLA decode could drop the newest one to three tokens when context length was below 2,044 and not a multiple of four. The kpool indexer placed the incomplete trailing pool at fixed columns 2044–2046, beyond the causal-length mask. The same problem affected early sparse-prefill rows when a batch's longest prefill exceeded 2,048 tokens.

The patch preserves the selected tokens by compacting affected rows before masking. Rows already handled correctly retain their original selections. The DCP2, physical-slot, and ckv-gather branches are unchanged. The patch verifies the original source hash and applies atomically.

The launcher now defaults to the v0.9.1 image. The target and serving defaults remain K4, DFlash2 K3, TP2 experts, DCP1 with MLA ownership, FP8 cache, and a 1M request limit.

## Validation

- Full Docker build, compatibility probes, and embedded structured-output tests passed.
- The actual indexer expansion, logical-to-physical mapping, and masking kernels reproduced the token loss on v0.9.0 at affected context lengths.
- On v0.9.1, all 18 tested lengths (1–2,050 tokens) preserve the selected tokens in contiguous, strided, and CUDA graph modes: **54 row checks passed**. Selection order is preserved; unaffected and dense rows remain unchanged. [Baseline receipt](tail-baseline.json), [fixed receipt](tail-fixed.json), [regression script](../../scripts/test-dcp1-kpool-tail.py).
- The new patch reapplication reports already applied; shell syntax and whitespace checks passed.

- Two-GPU K4 startup and release warmup passed; KV capacity remains **1,993,771 tokens**. [Runtime receipt](runtime-default.json).
- Frozen-document NLL: **1.17847**, versus v0.9.0's 1.17893. [Quality receipt](nll-frozen.json).
- A 32,768-token prefix replay returned the correct key on both passes, with **26,112 cached tokens** on the repeat. [Replay receipt](prefix-replay-default.json).
- Live tool-choice contracts: **16/16 passed**, covering thinking on/off and streaming/non-streaming. Chat and completion usage details remain available. [Tool receipts](tool-constraints.jsonl).

Live release checks used the default K4 profile. The full performance battery, long-reasoning completion study, DCP2, and alternate profiles were not rerun for this release. The README's performance tables remain the historical v0.8.0 baseline.

## Contributor measurements

The contributor tested K3.25 weights, with six GPQA prompts and 2,600 decoded tokens each, speculation off. Decode-versus-prefill KL fell from 0.066 to 0.010 below 2,044 tokens and from 0.031 to 0.019 at positions beyond 2,048. Early erroneous attention can affect cached states used by later tokens.

Under DFlash2 K3 defaults on K3.25, the contributor also reported 40 long GPQA generations at C8 without errors, similar throughput and acceptance, unchanged KV capacity, and tool-eval-bench scores of 159/176 and 163/176 versus 157/176 in both unpatched runs. These are contributor measurements, not new K4 release benchmarks. The PR does not establish that the fix resolves long-reasoning loops or exhaustion.

## Image

`ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx:v0.9.1`

Image index: `sha256:a9e985c09885710601b32ac86e81256aa1279b542caa345233ce331a816d7e07`. Source revision: `84572a81e0092796d396acf4c3f22d6aabc3724b`.

To upgrade, pull the latest repository changes, stop the old recipe container, and run `./start.sh`.
