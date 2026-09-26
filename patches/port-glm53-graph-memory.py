#!/usr/bin/env python3
"""Let VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0 skip the dry graph capture.

Upstream always dry-captures the largest graphs to estimate CUDA graph memory
and only uses the environment flag to decide whether the estimate is applied.
On this recipe the estimate reserves 0.71 GiB for graphs whose measured pool
is 0.23 GiB, and the dry capture replays B12x PCIe collectives while the two
ranks are not in scheduler lockstep. With the flag off, neither happens; the
remaining utilization headroom covers the real graph pool, which startup
logs as "CUDA graph pool memory ... (actual)".
"""

import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "v1/worker/gpu_worker.py"
old = """        if (
            current_platform.is_cuda_alike()
            and self.vllm_config.compilation_config.cudagraph_mode != CUDAGraphMode.NONE
        ):
            cudagraph_memory_estimate = self.model_runner.profile_cudagraph_memory()
"""
new = """        if (
            current_platform.is_cuda_alike()
            and self.vllm_config.compilation_config.cudagraph_mode != CUDAGraphMode.NONE
            and envs.VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS
        ):
            cudagraph_memory_estimate = self.model_runner.profile_cudagraph_memory()
"""
source = path.read_text(encoding="utf-8")
if new not in source:
    if source.count(old) != 1:
        raise RuntimeError(f"{path}: graph-memory profiling anchor drift")
    source = source.replace(old, new)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
print("GLM-5.3 graph-memory profiling switch applied")
