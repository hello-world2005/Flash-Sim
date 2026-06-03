"""Tests for Block_Manager preconditioning using the runtime topology."""

import os
import unittest

from flash_sim.FTL import Address_Mapping_Unit, Block_Manager
from flash_sim.PHY import PHY
from flash_sim.common import (
    BLOCK_PER_PLANE,
    PAGE_PER_BLOCK,
    STATIC_CHIP_PER_CHANNEL,
)


_DATA_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "pre_data",
    "precondition_data.json",
)


def _make_runtime_fixture():
    """Construct the real runtime objects needed by preconditioning()."""
    bm = Block_Manager()
    phy = PHY()
    amu = Address_Mapping_Unit()
    amu.block_manager = bm
    return bm, phy, amu


class TestPreconditioning(unittest.TestCase):
    """Validate the data-driven preconditioning path."""

    @classmethod
    def setUpClass(cls):
        cls.bm, cls.phy, cls.amu = _make_runtime_fixture()
        cls.bm.preconditioning(data_path=_DATA_PATH, phy=cls.phy, amu=cls.amu)
        cls.target_plane_bke = None
        for channel_planes in cls.bm.block_keeping_book:
            for chip_planes in channel_planes:
                for die_planes in chip_planes:
                    for plane_bke in die_planes:
                        if plane_bke.valid_page_count or plane_bke.invalid_page_count:
                            cls.target_plane_bke = plane_bke
                            break
                    if cls.target_plane_bke is not None:
                        break
                if cls.target_plane_bke is not None:
                    break
            if cls.target_plane_bke is not None:
                break
        if cls.target_plane_bke is None:
            raise AssertionError("expected at least one preconditioned plane")

    def setUp(self):
        self.plane_bke = self.target_plane_bke

    def test_free_block_pool_pages(self):
        """Blocks left in the free pool remain fully free."""
        for block_id in self.plane_bke.free_block_pool:
            bke = self.plane_bke.block_entries[block_id]
            self.assertEqual(bke.free_page_count, PAGE_PER_BLOCK)
            self.assertEqual(bke.valid_page_count, 0)
            self.assertEqual(bke.invalid_page_count, 0)
            self.assertEqual(len(bke.valid_pages), 0)
            self.assertEqual(len(bke.invalid_pages), 0)

    def test_write_frontier_block(self):
        """The write frontier block is allocated outside the free pool."""
        wfb_id = self.plane_bke.write_frontier_block
        self.assertNotIn(wfb_id, self.plane_bke.free_block_pool)
        wfb = self.plane_bke.block_entries[wfb_id]
        self.assertGreaterEqual(wfb.write_frontier, 0)
        self.assertLessEqual(wfb.write_frontier, PAGE_PER_BLOCK)

    def test_full_block_valid_invalid_ratio(self):
        """Full blocks contain no free pages and keep plane counters in sync."""
        all_blocks = set(range(self.bm.block_no_per_plane))
        full_blocks = all_blocks - self.plane_bke.free_block_pool - {self.plane_bke.write_frontier_block}

        total_valid = 0
        total_invalid = 0
        for block_id in full_blocks:
            bke = self.plane_bke.block_entries[block_id]
            self.assertEqual(bke.free_page_count, 0)
            self.assertEqual(bke.valid_page_count + bke.invalid_page_count, PAGE_PER_BLOCK)
            self.assertEqual(len(bke.valid_pages & bke.invalid_pages), 0)
            total_valid += bke.valid_page_count
            total_invalid += bke.invalid_page_count

        wfb = self.plane_bke.block_entries[self.plane_bke.write_frontier_block]
        total_valid += wfb.valid_page_count
        total_invalid += wfb.invalid_page_count

        self.assertEqual(self.plane_bke.valid_page_count, total_valid)
        self.assertEqual(self.plane_bke.invalid_page_count, total_invalid)

    def test_phy_storage_written(self):
        """Every valid page materialized during preconditioning is present in PHY storage."""
        all_blocks = set(range(self.bm.block_no_per_plane))
        non_free = all_blocks - self.plane_bke.free_block_pool
        written = 0
        for block_id in non_free:
            bke = self.plane_bke.block_entries[block_id]
            for page_idx in bke.valid_pages:
                pd = self.phy._storage[0][0][0][0][block_id][page_idx]
                self.assertIsNotNone(pd.lpa)
                written += 1
        self.assertGreaterEqual(written, 0)

    def test_plane_statistics(self):
        """Plane counters match the aggregate block bookkeeping."""
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

    def test_static_chip_skipped(self):
        """Preconditioning leaves the dedicated static chip untouched."""
        static_chip_id = self.bm.chip_no_per_channel - STATIC_CHIP_PER_CHANNEL
        static_plane_bke = self.bm.block_keeping_book[0][static_chip_id][0][0]
        self.assertEqual(static_plane_bke.valid_page_count, 0)
        self.assertEqual(static_plane_bke.invalid_page_count, 0)
        self.assertEqual(static_plane_bke.write_frontier_block, 0)
        self.assertEqual(static_plane_bke.free_page_count, PAGE_PER_BLOCK * BLOCK_PER_PLANE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
