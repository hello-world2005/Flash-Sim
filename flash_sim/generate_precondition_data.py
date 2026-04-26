# -*- coding: utf-8 -*-
"""
生成 precondition_data.json 脚本。

读取 common.py / config.py 中的 Flash 几何配置，在合法 LPA 范围内随机生成
num_data 条预调试数据，输出到 pre_data/precondition_data.json。

用法：
    python generate_precondition_data.py [num_data] [output_path]

参数：
    num_data     要生成的条目数量（默认 100）
    output_path  输出文件路径（默认 ../pre_data/precondition_data.json）
"""

import sys
import os
import json
import random

# 确保能导入 flash_sim 包
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_pkg_dir)
sys.path.insert(0, _root_dir)

from flash_sim.config import FlashGeometry
from flash_sim.common import (
    CHANNEL_NO,
    CHIP_PER_CHANNEL,
    DIE_PER_CHIP,
    PLANE_PER_DIE,
    BLOCK_PER_PLANE,
    PAGE_PER_BLOCK,
    SECTOR_PER_PAGE,
    STATIC_CHIP_PER_CHANNEL,
    GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
)


def compute_max_lpa() -> int:
    """
    计算非 static chip 范围内的最大合法 LPA（exclusive）。

    LPA 地址映射规则（低位到高位）：
        page_in_block < block_in_plane < plane_in_die < die_in_chip < chip < channel
    """
    non_static_chips = CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL
    max_lpa = (
        CHANNEL_NO
        * non_static_chips
        * DIE_PER_CHIP
        * PLANE_PER_DIE
        * BLOCK_PER_PLANE
        * PAGE_PER_BLOCK
    )
    return max_lpa


def compute_safe_num_data(max_lpa: int) -> int:
    """
    计算单个 plane 可容纳的安全数据量上限，用于提示用户。

    单个 plane 的 block 数：BLOCK_PER_PLANE
    可用于 full block 的数量上限：BLOCK_PER_PLANE - GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD - 1
    """
    geometry = FlashGeometry()
    valid_per_full_block = max(1, int(PAGE_PER_BLOCK * geometry.valid_invalid_ratio))
    max_full_blocks_per_plane = BLOCK_PER_PLANE - GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD - 1
    # 全局 plane 数
    total_planes = (
        CHANNEL_NO
        * (CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL)
        * DIE_PER_CHIP
        * PLANE_PER_DIE
    )
    safe_per_plane = max_full_blocks_per_plane * valid_per_full_block + (valid_per_full_block - 1)
    return safe_per_plane * total_planes


def generate_precondition_data(num_data: int, seed: int = None) -> list:
    """
    在合法 LPA 范围内随机生成 num_data 条预调试数据。

    每条数据格式：
        {
            "lpa": <int>,
            "valid_bitmap": [<int>, ...],   # 长度 = SECTOR_PER_PAGE，0 或 1
            "data": [<int>, ...]            # 长度 = SECTOR_PER_PAGE，模拟扇区数据
        }
    """
    if seed is not None:
        random.seed(seed)

    max_lpa = compute_max_lpa()
    if num_data > max_lpa:
        raise ValueError(
            f"num_data={num_data} 超过总 LPA 空间 {max_lpa}，请减少 num_data。"
        )

    safe_total = compute_safe_num_data(max_lpa)
    if num_data > safe_total:
        print(
            f"[WARNING] num_data={num_data} 可能导致某些 plane overfull（安全上限约 {safe_total}）。"
            f"建议减少 num_data，或增大 BLOCK_PER_PLANE / 减小 valid_invalid_ratio。",
            file=sys.stderr,
        )

    # 随机采样不重复的 LPA
    lpa_pool = random.sample(range(max_lpa), num_data)
    lpa_pool.sort()  # 排序方便调试查看

    records = []
    for lpa in lpa_pool:
        # valid_bitmap：每个扇区是否有效，全部置 1 表示整页有效
        valid_bitmap = [1] * SECTOR_PER_PAGE
        # data：每个扇区的伪数据，用随机整数填充
        data = [random.randint(0, 0xFFFF) for _ in range(SECTOR_PER_PAGE)]
        records.append({
            "lpa": lpa,
            "valid_bitmap": valid_bitmap,
            "data": data,
        })

    return records


def main():
    num_data = int(sys.argv[1]) if len(sys.argv) > 1 else 100

    default_output = os.path.join(_root_dir, 'pre_data', 'precondition_data.json')
    output_path = sys.argv[2] if len(sys.argv) > 2 else default_output

    print(f"[generate_precondition_data] Flash geometry summary:")
    print(f"  CHANNEL_NO          = {CHANNEL_NO}")
    print(f"  CHIP_PER_CHANNEL    = {CHIP_PER_CHANNEL}  (non-static: {CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL})")
    print(f"  DIE_PER_CHIP        = {DIE_PER_CHIP}")
    print(f"  PLANE_PER_DIE       = {PLANE_PER_DIE}")
    print(f"  BLOCK_PER_PLANE     = {BLOCK_PER_PLANE}")
    print(f"  PAGE_PER_BLOCK      = {PAGE_PER_BLOCK}")
    print(f"  SECTOR_PER_PAGE     = {SECTOR_PER_PAGE}")
    max_lpa = compute_max_lpa()
    print(f"  Max valid LPA       = {max_lpa - 1}  (total {max_lpa} LPAs)")
    print(f"  GC free threshold   = {GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD}")
    print(f"  valid_invalid_ratio = {FlashGeometry().valid_invalid_ratio}")
    print()
    print(f"[generate_precondition_data] Generating {num_data} entries -> {output_path}")

    records = generate_precondition_data(num_data)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(records, f, indent=2)

    print(f"[generate_precondition_data] Done. {len(records)} entries written.")


if __name__ == '__main__':
    main()
