# v0.9.0 — cache correctness and visible prefix hits

This release merges all three open PRs: [#3](https://github.com/tpurtell/glm-5.3-flash-ext3-2x-rtx/pull/3), [#4](https://github.com/tpurtell/glm-5.3-flash-ext3-2x-rtx/pull/4), and [#5](https://github.com/tpurtell/glm-5.3-flash-ext3-2x-rtx/pull/5).

- Fix kpool prefill tail addressing to honor the indexer cache's padded block stride (upstream vLLM #57477).
- Size the kpool tail ring for speculative drafts, preventing rejected drafts from overwriting committed keys (upstream vLLM #58454).
- Enable `prompt_tokens_details` in API usage, including `cached_tokens`.
- Add `GLM53_DFLASH_BOUNDARY_LOOKUP=1` to reuse prefixes on the aligned DFlash2 boundary. This remains off by default and requires `SPECULATIVE_METHOD=dflash2`.
- Resolve the older boundary-lookup PR against the v0.8.0 Docker patch chain and README, retaining MLA ownership, compact records, and draft-slot sharing.
- Update the launcher's default image to v0.9.0 and correct the stale model-profile test to expect the existing 1M K4 context limit.

The target remains Brandon's uniform-K4 checkpoint, with DFlash2 K3, TP2 experts, MLA layer ownership, FP8 cache, and a 1M request limit. The performance tables in the README remain the measured v0.8.0 baseline.

## Validation

- Full Docker build, compatibility probes, and embedded structured-output tests passed.
- Upstream kpool GPU regression tests: **33 passed, 1 skipped** (ROCm-only), on an RTX PRO 6000 Blackwell. [JUnit receipt](kpool-tests.xml).
- All three new patch scripts reapply idempotently on the release image.
- Ten model/context-profile cases, adaptive-MTP policy checks, shell syntax, and boundary-lookup argument rejection checks passed.

- Two-GPU startup and release warmup passed; the K4 KV pool remains **1,993,771 tokens**. [Runtime receipt](runtime-default.json).
- Frozen-document NLL: **1.17893**, compared with v0.8.0's 1.17864. [Quality receipt](nll-frozen.json).
- A 32,768-token prefix replay returned the correct retrieval key on both passes and reported **26,112 cached tokens** on the second pass. [Replay receipt](prefix-replay-default.json).
- Live tool-choice contracts: **16/16 passed**, covering thinking on/off and streaming/non-streaming. All chat usage responses included prompt-token details. [Tool receipts](tool-constraints.jsonl).

Live checks used the default profile with boundary lookup off. The opt-in boundary path passed build, syntax, idempotence, and launcher validation; its performance figures in the README are the contributor's downstream DCP2 measurements. The full v0.8.0 performance battery and alternate profiles were not rerun.

The upstream kpool test source is pinned to `vllm-project/vllm@6dd813e0b7ca8314fed28c13a73cfb5fc5310163`, file `tests/kernels/test_kpool_decode_update_batched.py`, SHA-256 `698d688693f37ba8a392a1142ece58cd10ff22de79cfb7164a9d9888d9e63448`.

## Image

`ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx:v0.9.0`

Image index: `sha256:f36dfb876da9393c95ca738ed912ca73905d0211004ce5c09b9ae312ed0f5cc9`. Image source revision: `c512b93ee4577f16e0e9396ad001d5203324bf15`.

To upgrade, pull the latest repository changes and run `./start.sh` after stopping the old recipe container. To try aligned prefix reuse, run `GLM53_DFLASH_BOUNDARY_LOOKUP=1 ./start.sh`.
