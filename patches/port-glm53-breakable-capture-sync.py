#!/usr/bin/env python3
"""Synchronize capture-time eager segments of breakable CUDA graphs.

During breakable capture, ``add_eager`` ends the current graph segment and
runs the eager function (KDA recurrence, MLA core, sparse indexer) on real
GPU work before the next segment begins capturing. Those eager kernels were
left in flight while the next segment captured and while ``_capture``'s
pre-capture ``gc.collect()`` / ``empty_cache()`` ran for the next batch
descriptor. With ``expandable_segments`` the release can unmap pool pages an
in-flight eager kernel still reads, which surfaced as intermittent "Warp MMU
Fault" / illegal-address errors in a BF16 elementwise kernel during capture.
DCP2 hid the race because its eager MLA segment contained blocking
collectives; ``CUDA_LAUNCH_BLOCKING=1`` hid it the same way.

The synchronization happens only while capturing. Graph replay, which calls
the recorded eager function directly, is unchanged.
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
path = root / "compilation/breakable_cudagraph.py"
replace_once(
    path,
    """        self._end_segment()
        result = fn()
        self.segments.append(fn)""",
    """        self._end_segment()
        result = fn()
        # Capture-time only: retire the eager kernels before the allocator or
        # the next segment can recycle the memory they read.
        torch.cuda.current_stream().synchronize()
        self.segments.append(fn)""",
    "capture-time eager synchronization",
)
replace_once(
    path,
    """        gc.collect()
        torch.accelerator.empty_cache()
        # Sync the offloader's copy stream before capture so any in-flight""",
    """        torch.accelerator.synchronize()
        gc.collect()
        torch.accelerator.empty_cache()
        # Sync the offloader's copy stream before capture so any in-flight""",
    "pre-capture synchronization",
)
print("GLM-5.3 breakable-capture synchronization patch applied")
