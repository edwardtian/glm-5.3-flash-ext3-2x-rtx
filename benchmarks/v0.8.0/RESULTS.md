# GLM-5.3 Flash v0.8.0 serving measurements

Generated from the linked raw receipts by `scripts/summarize-results.py`. Values are median (minimum–maximum) unless noted. Decode rates use the recipe's N−1 convention over each stream's first-to-last token window and exclude prefill (TTFT). Speculative acceptance comes from vLLM `/metrics` counter deltas.

## Reasoning-coding decision metric

Thinking enabled at the template default effort, temperature 1.0, top-p 0.95, fixed per-request seeds, four rotating coding prompts (LRU+TTL cache with tests, async task-runner fix, PostgreSQL migration with rollback, typed refactor). Each concurrency runs one warmup batch and three measured batches of C simultaneous requests. Per-request decode counts all generated tokens (reasoning and content). The **score** is the mean of the C1, C2 and C4 per-request medians; tuning decisions are ranked by it.

| Rank | Profile | Score, tok/s | Δ vs v0.8.0 default | C1 per-request | C2 per-request | C4 per-request | Draft acceptance | Finish reasons | Raw |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | v0.8.0 default | **108.59** | — | 140.94 (126.57–141.32) | 107.84 (100.27–112.04) | 76.98 (69.72–81.85) | 49.81% | length 21 | [receipt](reasoning-coding.json) |

| Profile | C | Per-request tok/s | Aggregate tok/s | TTFT, s | Reasoning tokens | Content tokens | Acceptance | Mean accepted length |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v0.8.0 default | 1 | 140.94 (126.57–141.32) | 140.94 (126.57–141.32) | 0.106 (0.106–0.109) | 2048 (2048–2048) | 0 (0–0) | 49.29% | 2.479 |
| v0.8.0 default | 2 | 107.84 (100.27–112.04) | 205.62 (200.55–211.30) | 0.171 (0.106–0.264) | 2048 (2048–2048) | 0 (0–0) | 49.63% | 2.489 |
| v0.8.0 default | 4 | 76.98 (69.72–81.85) | 287.92 (278.88–290.00) | 0.305 (0.108–0.306) | 2048 (2048–2048) | 0 (0–0) | 50.03% | 2.501 |

Aggregate divides the batch's summed N−1 tokens by its global first-to-last token window. With a 2048-token cap many requests end inside reasoning (`length`); the rate is decode speed, not task success. Prompts repeat across batches, so TTFT can include prefix-cache hits.

## Profiles

| Profile | Served model | Speculation | Max model len | Max seqs | GPU memory fraction | KV cache | Parallelism | GPUs | Image | Recipe | Configuration |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v0.8.0 default | brandonmusic/GLM-5.3-Flash-tr3-4bpw | dflash K3 | 1048576 | 16 | 0.970 | fp8_ds_mla | TP2/DCP1 | 2× NVIDIA RTX PRO 6000 Blackwell Workstation Edition @ 400.00 W | sha256:e4d37a01da91 | d14b6374dc+dirty | [runtime](runtime.json) [suite](suite.json) |

## Per-GPU placement and memory ledger

From `runtime.json` (`capture-runtime.py`): vLLM startup memory lines per worker process, the GPU inventory at capture time, and every placement-ledger line (`glm53-placement` logger or `layer owner`).

### v0.8.0 default

| Process | TP rank | Weights GiB | Load s | Profiling s | Available KV GiB | Graph GiB | KV cache tokens | Max concurrency | Placement lines |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Worker_TP0 | 0 | 81.36 | 53.6 | — | 7.39 | 0.52 | — | — | 1 |
| Worker_TP1 | 1 | 81.91 | 53.3 | — | — | 0.51 | — | — | 1 |
| EngineCore | — | — | — | — | — | — | 1,993,771 | 1.90× @ 1,048,576 | 1 |

| GPU | Name | Memory used / total MiB | Power limit W | SM clock cur/max MHz | Mem clock cur/max MHz | App clocks gfx/mem MHz | Driver | PCIe |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | NVIDIA RTX PRO 6000 Blackwell Workstation Edition | 95788 / 97887 | 400.00 | 2812/3090 | 13365/14001 | [Requested functionality has been deprecated]/[Requested functionality has been deprecated] | 595.91.07 | Gen5 x16 |
| 1 | NVIDIA RTX PRO 6000 Blackwell Workstation Edition | 95106 / 97887 | 400.00 | 2842/3090 | 13365/14001 | [Requested functionality has been deprecated]/[Requested functionality has been deprecated] | 595.91.07 | Gen5 x16 |

Placement ledger (3 lines):

```text
(Worker_TP1 pid=620) INFO 09-26 11:42:01 [placement.py:287] glm53-placement ledger {"gib": {"dense_mlp": 0.422, "draft": 1.533, "embed": 0.981, "kda_attention": 4.398, "layer_other": 0.133, "lm_head": 0.591, "mla_attention": 1.163, "other": 0.0, "routed_experts": 0.0, "router": 0.092, "shared_experts": 0.984}, "mla_owned": [27, 31, 35, 39, 43], "rank": 1, "torch_allocated_gib": 81.934, "total_gib": 10.297}
(Worker_TP0 pid=546) INFO 09-26 11:42:02 [placement.py:287] glm53-placement ledger {"gib": {"dense_mlp": 0.422, "draft": 1.533, "embed": 0.201, "kda_attention": 4.398, "layer_other": 0.133, "lm_head": 0.591, "mla_attention": 1.396, "other": 0.0, "routed_experts": 0.0, "router": 0.092, "shared_experts": 0.984}, "mla_owned": [3, 7, 11, 15, 19, 23], "rank": 0, "torch_allocated_gib": 81.387, "total_gib": 9.75}
(EngineCore pid=370) INFO 09-26 11:42:21 [kv_cache_utils.py:2284] glm53-placement kv-capacity {"num_blocks": 540, "tensor_bytes": 7910369280, "groups": [{"spec": "MLAAttentionSpec", "layers": 12, "block_size": 4352, "page_bytes": 143616, "blocks_per_max_request": 241}, {"spec": "KpoolTailSpec", "layers": 6, "block_size": 4, "page_bytes": 143616, "blocks_per_max_request": 1}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 5, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "MambaSpec", "layers": 4, "block_size": 4352, "page_bytes": 2297856, "blocks_per_max_request": 5}, {"spec": "SlidingWindowSpec", "layers": 5, "block_size": 1088, "page_bytes": 2297856, "blocks_per_max_request": 7}]}
```

## Seven content workloads: C1

One warmup and three measured responses per workload, temperature zero, thinking closed at render time. The weighted blend is total N−1 tokens divided by total decode time. Rates include failed output contracts and are not successful-task throughput.

| Workload | v0.8.0 default tok/s | Contract |
| --- | --- | --- |
| code | 204.68 (199.46–208.43) | 3/3 |
| math | 191.96 (191.78–208.89) | 3/3 |
| fable | 114.66 (113.98–122.48) | 2/3 |
| hello | 174.71 (174.44–178.29) | 3/3 |
| topic | 149.59 (149.06–163.17) | 3/3 |
| structured-json | 181.47 (180.85–190.90) | 3/3 |
| multilingual | 124.33 (122.97–129.56) | 3/3 |

| Profile | Weighted blend | Draft acceptance | Mean acceptance length | Raw |
| --- | --- | --- | --- | --- |
| v0.8.0 default | 149.83 | 54.13% | 2.624 | [responses and timings](seven.jsonl) |

The code contract checks syntax and required assertions without executing code; the Chinese terminology check is a literal-phrase proxy. Rejected responses remain in the raw files.

## Orchid repetition: C1

Exactly 100 space-separated `orchid` words requested with a 1500-token cap; one warmup and five measured runs. A fast incorrect repetition is not a task success.

| Profile | Word counts | Exact contract | Decode tok/s | Raw |
| --- | --- | --- | --- | --- |
| v0.8.0 default | 100, 100, 100, 100, 100 | 5/5 | 236.51 (236.04–238.06) | [responses](orchid.jsonl) |

## Sampled prose: independent clients

Each client requests 256 forced output tokens at temperature 0.7 with fixed per-client seeds and thinking closed at render time. Two full warmups and three measurements per concurrency. Aggregate rate divides the summed N−1 tokens by the batch's first-to-last token window; overlap counts come from client stream intervals.

| Clients | v0.8.0 default aggregate tok/s | Overlap | Acceptance |
| --- | --- | --- | --- |
| 1 | 113.16 (113.03–117.85) | 1 | 34.48% |
| 2 | 177.59 (170.85–187.04) | 2 | 36.05% |
| 4 | 265.74 (261.14–275.81) | 4 | 36.81% |
| 8 | 376.60 (369.30–378.25) | 8 | 37.50% |
| 16 | 403.10 (402.95–411.34) | 16 | 36.38% |

Raw: [v0.8.0 default](clients.json).

## Prefill matrix: C1

Exact prompt lengths, unique first cache blocks, three measurements after a warmup at each depth. Effective prompt tok/s includes server tokenization and the first output token handoff; it is not isolated GPU prefill time.

| Prompt tokens | v0.8.0 default tok/s | v0.8.0 default TTFT, s |
| --- | --- | --- |
| 2,048 | 4609.7 (4588.6–4917.8) | 0.444 (0.416–0.446) |
| 8,192 | 5039.1 (5035.9–5067.5) | 1.626 (1.617–1.627) |
| 32,768 | 5075.9 (5065.1–5086.3) | 6.456 (6.442–6.469) |
| 65,536 | 5048.4 (5032.7–5062.3) | 12.981 (12.946–13.022) |
| 131,072 | 4983.7 (4975.1–4986.1) | 26.300 (26.288–26.346) |
| 262,144 | 4844.5 (4837.2–4846.1) | 54.112 (54.093–54.194) |
| 524,288 | 4601.5 (4601.2–4607.2) | 113.938 (113.796–113.946) |

Raw: [v0.8.0 default](prefill.json).

## Context and decode scaling: C1

Exact-length synthetic filler followed by 256 forced output tokens; one warmup and three measurements per depth. Serving capacity and speed, not long-context quality.

| Prompt tokens | v0.8.0 default decode tok/s | v0.8.0 default TTFT, s | Acceptance |
| --- | --- | --- | --- |
| 2,048 | 227.81 (227.17–229.72) | 0.425 (0.419–0.427) | 100.00% |
| 8,192 | 234.01 (233.98–234.18) | 1.587 (1.585–1.613) | 100.00% |
| 32,768 | 236.02 (235.76–236.21) | 6.382 (6.378–6.418) | 100.00% |
| 131,072 | 237.97 (235.75–238.59) | 26.177 (26.136–26.188) | 100.00% |
| 262,144 | 243.97 (243.50–247.38) | 54.128 (54.105–54.144) | 100.00% |
| 524,288 | 253.16 (252.87–257.11) | 113.868 (113.843–114.100) | 100.00% |

Raw: [v0.8.0 default](context.jsonl).

v0.8.0 default returned 256 of 256 requested tokens after a 1,048,288-token prompt (configured max model length 1,048,576) at 272.30 tok/s: [receipt](context-boundary.jsonl).

## Reference coding task across retained KV depths: C1

The async task-runner prompt, thinking closed, temperature 0.2, fixed seed, 256 forced output tokens; ordinary filler precedes the task. Prompts repeat, so these TTFTs are not uncached prefill. The token cap is not a code-correctness test.

| Prompt depth | v0.8.0 default decode tok/s | Acceptance |
| --- | --- | --- |
| Task only | 177.46 (175.25–179.49) | 76.50% |
| 8,192 | 181.17 (175.00–181.98) | 80.00% |
| 32,768 | 175.62 (173.30–178.25) | 76.92% |
| 65,536 | 178.45 (173.72–183.57) | 77.49% |
| 128,000 | 176.38 (170.00–190.04) | 74.68% |

Raw: [v0.8.0 default](code-agent.json).

## Functional and tool checks

| Profile | API tool choices | Numbered images per request | Retrieval by filler tokens | Multi-needle |
| --- | --- | --- | --- | --- |
| v0.8.0 default | 16/16 | skipped: vision disabled | 8,192: 3/3, 240,000: 3/3, 1,000,000: 3/3 | 6/6 at 1,000,000 |

API checks cover required/named/auto/none tool choices, thinking off/on via `chat_template_kwargs.enable_thinking`, and streaming/non-streaming. Retrieval places one random key early, midway and late in exact-length filler archives with thinking closed. Image checks read ordered numbers from 1, 4 and 16 images and require a 17-image rejection; they are smoke tests, not broad vision evaluation.

| Profile | Status | Full suite points | Hard Mode points | Raw |
| --- | --- | --- | --- | --- |
| v0.8.0 default | completed | 159/176 | 34/38 | [full tool traces](tools.md) |

v0.8.0 default evaluator-flagged cases:

- TC-43 (Omitted Required Parameter): Called web_search with an empty query — violated required parameter constraint.

Tool-eval-bench is pinned at `cf54b4bfe705f12f71e8866f10730572497c8105`: all 88 cases including 19 Hard Mode, eight parallel cases (C8), temperature zero, template-default thinking, one trial, at most eight turns. Points are not a normalized cross-setup score.
