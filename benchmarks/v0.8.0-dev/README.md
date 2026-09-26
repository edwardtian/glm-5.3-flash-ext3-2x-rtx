# v0.8.0 development arms

Each directory holds one server configuration's raw `reasoning-coding.json`
and, where captured, its `server.log`. `scripts/compare-arms.py` renders the
table below. All arms ran on the same two RTX PRO 6000 Blackwell GPUs at a
400 W limit with Brandon's uniform-K4 checkpoint
(`brandonmusic/GLM-5.3-Flash-tr3-4bpw@a5fee92`).

The decision metric is the **reasoning-coding score**: the mean of the C1,
C2 and C4 median per-request decode rates on four realistic coding prompts
with thinking enabled (`scripts/benchmark-reasoning-coding.py`). It is noisy
at the level of a few percent because sampling is on (temperature 1.0).

## Arms that stand

| Arm | Change measured | Score | C1 | C2 | C4 | Pool |
|---|---|---:|---:|---:|---:|---|
| `baseline-v071-k4` | published v0.7.1 image: DCP2, EP2, K5, 656 B records | 99.6 | 131.4 | 100.5 | 67.0 | 335,088 @ 256K |
| `owner25-nccl` | MLA ownership, DCP off, NCCL all-reduce | 100.9 | 131.8 | 101.4 | 69.6 | |
| `owner25-b12x-draftslots` | + B12x all-reduce, capture sync, draft slot sharing | 105.4 | 137.2 | 107.1 | 71.7 | 687,135 @ 256K |
| `owner25-k3` | DFlash2 K3 | 104.2 | 133.1 | 104.1 | 75.5 | 779,767 @ 256K |
| `owner25-k7` | DFlash2 K7 | 96.5 | 132.0 | 93.9 | 63.7 | 615,858 @ 256K |
| `owner25-mtp3` | checkpoint MTP layer, static K3 | 91.4 | 113.6 | 92.9 | 67.6 | 789,881 @ 256K |
| `owner25-mtp-adaptive` | checkpoint MTP layer, adaptive K1–K5 | 72.3 | 97.5 | 80.6 | 38.8 | 782,982 @ 256K |
| `fixed-default` | **release default**: latest fork, TP2 experts, K3, 528 B records, 1M, vision off | **108.3** | 139.6 | 108.1 | 77.3 | 1,993,771 @ 1M |
| `fixed-ep2` | release default with EP2 experts | 103.5 | 128.2 | 106.8 | 75.6 | 2,030,692 @ 1M |
| `fixed-k5` | release default with DFlash2 K5 | 107.6 | 149.0 | 103.1 | 70.8 | 1,893,066 @ 1M |

The `owner25-*` arms used the v0.7.1 fork pin (`fe054789`) and the EP2
expert path; the `fixed-*` arms use fork `7fcc094e` with the extend-output fix.

## Arms that are invalid

`latest-owner25-k5`, `latest-owner25-k5-nope`, `final-candidate-1m` and
`final-tp-experts` ran the first latest-fork port, whose sparse-MLA **prefill**
path requested a head-major output view that the current fork's extend kernel
no longer produces. Prompt-token attention was wrong (NaN from the first MLA
layer in eager serving), which inflated both acceptance and decode rates. Their
receipts are kept only as the evidence for that bug; none of their numbers are
used. The teacher-forced NLL probe (`scripts/test-prompt-nll.py`) exposed it:

| Server | Token-weighted mean NLL |
|---|---:|
| v0.7.1 published image | 1.174 |
| v0.7.1 fork + MLA ownership | 1.178 |
| first latest-fork port (broken extend) | 3.71 |
| latest fork, extend fixed, release default | 1.178 |
| release default + vision | 1.179 |
| release default + NVFP4 KV cache | 1.176 |

## Option checks

`option-vision` and `option-nvfp4` are startup, sanity, NLL and needle checks
of the two launch options; the full battery was run only on the default.
