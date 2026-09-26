# v0.8.0

**Default target:** `brandonmusic/GLM-5.3-Flash-tr3-4bpw@a5fee92` (Brandon's
uniform-K4 checkpoint, renamed from `GLM-5.3-Flash-EXL3-4bpw`; same weights).

**Headline, same K4 checkpoint, published v0.7.1 image vs v0.8.0**

| | v0.7.1 | v0.8.0 |
|---|---:|---:|
| Reasoning-coding score (C1/C2/C4 mean) | 99.6 tok/s | 108.6 tok/s |
| KV pool | 335,088 @ 256K | 1,993,771 @ 1M |
| Prefill, 128K prompt | 4,549 tok/s | 4,984 tok/s |
| Teacher-forced NLL (frozen text) | 1.1743 | 1.1786 |
| Tool-eval-bench, 88 cases C8 | 157/176 (v0.7.0) | 159/176 |

**Changes**

- MLA layer ownership replaces DCP2; KDA and routed experts are TP2.
- Current SparkInfer/B12x fork `7fcc094e` via prepared plan/bind/run APIs.
- 528-byte GLM_NEXT MLA cache records; DFlash2 draft cache inside MLA slots;
  uneven embedding split; graph-memory dry-capture skip; 1M default limit.
- DFlash2 K3 and TP2 experts chosen by the reasoning-coding metric.
- Vision off by default (`LANGUAGE_MODEL_ONLY=0` restores it: 1.73M pool);
  NVFP4 cache option (2.62M pool).
- Z.ai's corrected chat template is baked into the image and used for all
  profiles.
- Fixes: capture-time race in breakable CUDA graphs with DCP off; prefill
  output layout under the current fork's sparse-MLA extend kernel.
- The EXL3 source files are vendored; rebuilds no longer pull an 11 GB image.

**Not requalified:** K3 and K3.25 profiles (carried over from v0.7.x).

Image: `ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx:v0.8.0`
(`sha256:e4d37a01…2423`). Full results: `benchmarks/v0.8.0/RESULTS.md`.
