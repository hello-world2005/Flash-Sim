"""Focused tests for the GC/WL unit behavior."""

from types import SimpleNamespace

from flash_sim.FTL import Block_Manager, FTL, GC_WL_Unit, TSU
from flash_sim.PHY import PHY
from flash_sim.common import (
    FlashAddress,
    PAGE_PER_BLOCK,
    SECTOR_PER_PAGE,
    Transaction,
    TransactionType,
)


class _FakeTSU:
    def __init__(self, phy=None):
        self.phy = phy
        self.prepared = 0
        self.scheduled = 0
        self.submitted: list[Transaction] = []

    def Prepare_trans_submission(self):
        self.prepared += 1

    def Submit_trans(self, tr: Transaction):
        self.submitted.append(tr)

    def Schedule(self):
        self.scheduled += 1


def _make_gc_wl_fixture(with_phy: bool = False):
    bm = Block_Manager()
    unit = GC_WL_Unit()
    phy = PHY() if with_phy else None
    fake_tsu = _FakeTSU(phy=phy)
    unit.block_manager = bm
    unit.tsu = fake_tsu
    unit.address_mapping_unit = SimpleNamespace(
        gmt={},
        cmt=SimpleNamespace(cache={}),
    )
    bm.gc_wl_unit = unit
    return bm, unit, fake_tsu


def test_ftl_exposes_gc_wl_unit():
    """FTL wires the renamed GC/WL controller through the new attribute."""
    ftl = FTL()
    assert isinstance(ftl.gc_wl_unit, GC_WL_Unit)
    assert not hasattr(ftl, "gc_wl_manager")


def test_write_frontier_switch_uses_lowest_erase_free_block():
    """Dynamic WL picks the least-erased eligible free block on frontier rollover."""
    bm, _, _ = _make_gc_wl_fixture()
    plane_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=-1, page=-1)
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.write_frontier_block = 4
    plane_bke.block_entries[4].write_frontier = PAGE_PER_BLOCK
    plane_bke.free_block_pool = {1, 2, 3}
    plane_bke.block_entries[1].wl_level = 5
    plane_bke.block_entries[2].wl_level = 1
    plane_bke.block_entries[3].wl_level = 3

    addr = bm.get_write_frontier(plane_addr)

    assert addr.sub_plane == 2
    assert addr.page == 0
    assert 2 not in plane_bke.free_block_pool


def test_gc_victim_selection_skips_unsafe_blocks():
    """GC victim selection excludes frontier, protected, and in-flight user-program blocks."""
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=-1, page=-1)
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.free_block_pool = set()
    plane_bke.write_frontier_block = 1

    plane_bke.block_entries[1].invalid_page_count = 10
    plane_bke.block_entries[2].invalid_page_count = 9
    plane_bke.block_entries[3].invalid_page_count = 6
    plane_bke.block_entries[4].invalid_page_count = 8

    plane_bke.gc_wl_barrier_blocks.add(2)
    bm.lpa_protected_book[44] = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=44,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=0),
    )

    assert unit._pick_gc_victim_block(plane_addr) == 3


def test_on_erase_complete_triggers_static_wl_and_blocks_conflicting_writes():
    """Static WL submits a relocation chain and installs block barriers for conflicts."""
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    unit.static_wl_wear_gap_threshold = 1
    plane_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=-1, page=-1)
    plane_bke = bm.get_plane_bke(plane_addr)

    source_block = 3
    dest_block = 6
    plane_bke.write_frontier_block = 7
    plane_bke.free_block_pool = {dest_block}
    plane_bke.block_entries[dest_block].wl_level = 5
    plane_bke.block_entries[dest_block].free_page_count = PAGE_PER_BLOCK
    plane_bke.block_entries[dest_block].write_frontier = 0

    source_bke = plane_bke.block_entries[source_block]
    source_bke.wl_level = 0
    source_bke.valid_pages = {0, 1}
    source_bke.valid_page_count = 2
    source_bke.free_page_count = PAGE_PER_BLOCK - 2
    plane_bke.valid_page_count = 2

    for page_idx, lpa in enumerate((101, 102)):
        pd = fake_tsu.phy._storage[0][0][0][0][source_block][page_idx]
        pd.lpa = lpa
        pd.valid_bitmap = [1] * SECTOR_PER_PAGE

    unit.on_erase_complete(
        FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=1, page=0)
    )

    submitted_types = [tr.type for tr in fake_tsu.submitted]
    assert fake_tsu.prepared == 1
    assert fake_tsu.scheduled == 1
    assert submitted_types.count(TransactionType.GC_READ) == 2
    assert submitted_types.count(TransactionType.GC_WRITE) == 2
    assert submitted_types.count(TransactionType.GC_ERASE) == 1
    assert plane_bke.gc_wl_barrier_blocks == {source_block, dest_block}

    tsu = TSU()
    tsu.block_manager = bm
    blocked_write = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=999,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=dest_block, page=0),
    )
    assert tsu._transaction_blocked_by_barrier(blocked_write) is True
