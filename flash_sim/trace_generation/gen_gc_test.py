import json
import sys
from pathlib import Path

# 允许直接 `python gen_gc_test.py`：common 依赖顶层 `config`，需同时可见仓库根（flash_sim 包）与 flash_sim 目录
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FLASH_SIM_DIR = _REPO_ROOT / "flash_sim"
for _p in (_FLASH_SIM_DIR, _REPO_ROOT):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from flash_sim.common import (
    SECTOR_PER_PAGE,
    BLOCK_PER_PLANE,
    PAGE_PER_BLOCK,
    GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
)

def gen_gc_test(block_count: int, page_count: int) -> list[dict]:
    trace = []
    lha_cntr = 0
    for block_id in range(block_count):
        for page_id in range(page_count):
            trace.append({
                "type": "write",
                "time": 0,
                "start_lha": lha_cntr,
                "size": 1,
            })
            lha_cntr += SECTOR_PER_PAGE
    return trace

def write_trace_to_file(trace: list[dict], file_path: str):
    with open(file_path, "w") as f:
        json.dump(trace, f, indent=4)

if __name__ == "__main__":
    trace = gen_gc_test(BLOCK_PER_PLANE-GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD, PAGE_PER_BLOCK)
    print(f"Generated {len(trace)} reqs")
    write_trace_to_file(trace, f"E:/Files/Li_Meng/HBF/flash-sim/test_case/gc_test.json")
