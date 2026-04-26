"""
Block Manager Preconditioning 功能测试（数据驱动版）。

验证 preconditioning 函数的正确性：
1. free block pool 中所有 block 都是 free 的
2. write frontier block 的初始化
3. full block 中 valid/invalid page 的比例及 PHY storage 写入
4. plane 统计信息的准确性
5. static chip 跳过
"""

import sys
import os
import json
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flash_sim.FTL import Block_Manager, blockBKE, PlaneBKE
from flash_sim.PHY import PHY, PageData
from flash_sim.common import (
    PAGE_PER_BLOCK, GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
    BLOCK_PER_PLANE, STATIC_CHIP_PER_CHANNEL, SECTOR_PER_PAGE,
)
from flash_sim.config import FlashGeometry
import unittest

# 测试用 precondition_data.json 路径
_DATA_PATH = os.path.join(os.path.dirname(__file__), '..', 'pre_data', 'precondition_data.json')

# 固定参数：block_no 须 > GC_threshold + 1；plane/die 与数据生成脚本对齐
_BLOCK_NO = 128


def _make_bm_and_phy(block_no=_BLOCK_NO):
    """构造测试用的 Block_Manager 和 PHY（不连接 gc_wl_manager）。"""
    bm = Block_Manager(
        channel_no=1,
        chip_no_per_channel=2,   # chip 0: regular, chip 1: static
        die_no_per_chip=1,
        plane_no_per_die=1,
        block_no_per_plane=block_no,
        pages_per_block=PAGE_PER_BLOCK,
    )
    # 构造一个最小 PHY，仅用于存储检查
    class _MinPHY:
        def __init__(self):
            self._storage = [
                [
                    [
                        [
                            [
                                [PageData() for _ in range(PAGE_PER_BLOCK)]
                                for _ in range(block_no)
                            ]
                            for _ in range(1)   # plane_no_per_die=1
                        ]
                        for _ in range(1)   # die_no_per_chip=1
                    ]
                    for _ in range(2)   # chip_no_per_channel=2
                ]
                for _ in range(1)   # channel_no=1
            ]
    return bm, _MinPHY()


def _load_data():
    with open(_DATA_PATH) as f:
        return json.load(f)


class TestPreconditioning(unittest.TestCase):
    """测试 Block Manager 数据驱动 preconditioning 功能。"""

    def setUp(self):
        self.bm, self.phy = _make_bm_and_phy()
        self.bm.preconditioning(data_path=_DATA_PATH, phy=self.phy, amu=None)
        self.plane_bke = self.bm.block_keeping_book[0][0][0][0]

    def test_free_block_pool_pages(self):
        """free block pool 中每个 block 的 page 都应全为 free。"""
        print("\n[TEST] test_free_block_pool_pages")
        for block_id in self.plane_bke.free_block_pool:
            bke = self.plane_bke.block_entries[block_id]
            self.assertEqual(bke.free_page_count, PAGE_PER_BLOCK,
                             f"Block {block_id} in free pool: free_page_count should be {PAGE_PER_BLOCK}")
            self.assertEqual(bke.valid_page_count, 0)
            self.assertEqual(bke.invalid_page_count, 0)
            self.assertEqual(len(bke.valid_pages), 0)
            self.assertEqual(len(bke.invalid_pages), 0)
        print(f"[OK] All {len(self.plane_bke.free_block_pool)} free blocks verified")

    def test_write_frontier_block(self):
        """write_frontier_block 不在 free_block_pool 内，frontier 值合法。"""
        print("\n[TEST] test_write_frontier_block")
        wfb_id = self.plane_bke.write_frontier_block
        self.assertNotIn(wfb_id, self.plane_bke.free_block_pool)
        wfb = self.plane_bke.block_entries[wfb_id]
        self.assertGreaterEqual(wfb.write_frontier, 0)
        self.assertLessEqual(wfb.write_frontier, PAGE_PER_BLOCK)
        print(f"[OK] Write frontier block: {wfb_id} (frontier: {wfb.write_frontier}/{PAGE_PER_BLOCK})")

    def test_full_block_valid_invalid_ratio(self):
        """full block 的 valid+invalid == PAGE_PER_BLOCK，free == 0。"""
        print("\n[TEST] test_full_block_valid_invalid_ratio")
        all_blocks = set(range(self.bm.block_no_per_plane))
        full_blocks = all_blocks - self.plane_bke.free_block_pool - {self.plane_bke.write_frontier_block}

        total_valid = 0
        total_invalid = 0
        for block_id in full_blocks:
            bke = self.plane_bke.block_entries[block_id]
            self.assertEqual(bke.free_page_count, 0,
                             f"Full block {block_id}: free_page_count should be 0")
            self.assertEqual(bke.valid_page_count + bke.invalid_page_count, PAGE_PER_BLOCK,
                             f"Full block {block_id}: valid+invalid should equal PAGE_PER_BLOCK")
            self.assertEqual(len(bke.valid_pages & bke.invalid_pages), 0,
                             f"Full block {block_id}: valid and invalid pages should not overlap")
            total_valid += bke.valid_page_count
            total_invalid += bke.invalid_page_count

        wfb = self.plane_bke.block_entries[self.plane_bke.write_frontier_block]
        total_valid += wfb.valid_page_count
        total_invalid += wfb.invalid_page_count

        self.assertEqual(self.plane_bke.valid_page_count, total_valid)
        self.assertEqual(self.plane_bke.invalid_page_count, total_invalid)
        print(f"[OK] Full blocks: {len(full_blocks)}, valid: {total_valid}, invalid: {total_invalid}")

    def test_phy_storage_written(self):
        """所有 valid page 对应的 PHY storage 都应写入 lpa 和 data。"""
        print("\n[TEST] test_phy_storage_written")
        all_blocks = set(range(self.bm.block_no_per_plane))
        non_free = all_blocks - self.plane_bke.free_block_pool
        written = 0
        for block_id in non_free:
            bke = self.plane_bke.block_entries[block_id]
            for page_idx in bke.valid_pages:
                pd = self.phy._storage[0][0][0][0][block_id][page_idx]
                self.assertIsNotNone(pd.lpa, f"Block {block_id} page {page_idx}: lpa should not be None")
                written += 1
        print(f"[OK] PHY storage written for {written} valid pages")

    def test_plane_statistics(self):
        """plane 统计数与各 block 实际值之和一致，总页数等于 BLOCK * PAGE。"""
        print("\n[TEST] test_plane_statistics")
        total_free = total_valid = total_invalid = 0
        for bke in self.plane_bke.block_entries:
            total_free += bke.free_page_count
            total_valid += bke.valid_page_count
            total_invalid += bke.invalid_page_count

        self.assertEqual(self.plane_bke.free_page_count, total_free)
        self.assertEqual(self.plane_bke.valid_page_count, total_valid)
        self.assertEqual(self.plane_bke.invalid_page_count, total_invalid)
        expected = PAGE_PER_BLOCK * self.bm.block_no_per_plane
        self.assertEqual(total_free + total_valid + total_invalid, expected)
        print(f"[OK] Total pages: {total_free + total_valid + total_invalid}/{expected}")

    def test_static_chip_skipped(self):
        """static chip 的 plane 不应被赋值（free_block_pool 仍是初始全集）。"""
        print("\n[TEST] test_static_chip_skipped")
        # chip 1 是 static chip（chip_no_per_channel=2, STATIC_CHIP_PER_CHANNEL=1）
        static_plane_bke = self.bm.block_keeping_book[0][1][0][0]
        # static chip 未做 preconditioning，valid/invalid count 都应为 0
        self.assertEqual(static_plane_bke.valid_page_count, 0,
                         "Static chip plane should have 0 valid pages")
        self.assertEqual(static_plane_bke.invalid_page_count, 0,
                         "Static chip plane should have 0 invalid pages")
        # write_frontier_block 保持初始值 0
        self.assertEqual(static_plane_bke.write_frontier_block, 0,
                         "Static chip plane write_frontier_block should remain 0")
        print("[OK] Static chip detection correct")

if __name__ == '__main__':
    unittest.main(verbosity=2)
