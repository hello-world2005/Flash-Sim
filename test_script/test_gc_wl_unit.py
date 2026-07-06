"""Focused tests for the GC/WL unit behavior."""

from types import SimpleNamespace

import pytest

from flash_sim.FTL import Address_Mapping_Unit, Block_Manager, FTL, GC_WL_Unit, TSU
from flash_sim.PHY import PHY, PageType
from flash_sim.common import (
    FlashAddress,
    GTDEntry,
    INVALID_LPA,
    INVALID_PPA,
    LPA_NO_PER_MAPPING_PAGE,
    PAGE_PER_BLOCK,
    SECTOR_PER_PAGE,
    SET_REQUEST_LATENCY_RECORDER,
    Transaction,
    TransactionType,
    cmt_entry,
)
from flash_sim import utils
from flash_sim.config import RuntimeConfig
from flash_sim.request_latency_report import RequestLatencyRecorder


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
        on_host_write_complete=lambda tr: None,
    )
    bm.gc_wl_unit = unit
    return bm, unit, fake_tsu


def _make_gc_mapping_fixture():
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    amu = Address_Mapping_Unit()
    amu.block_manager = bm
    amu.tsu = fake_tsu
    unit.address_mapping_unit = amu
    return bm, unit, fake_tsu, amu


def _plane_addr() -> FlashAddress:
    return FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=-1, page=-1)


def _assert_plane_bookkeeping_consistent(bm: Block_Manager, plane_addr: FlashAddress) -> None:
    plane_bke = bm.get_plane_bke(plane_addr)
    total_free = total_valid = total_invalid = 0
    for block_id, bke in enumerate(plane_bke.block_entries):
        assert bke.valid_pages.isdisjoint(bke.invalid_pages)
        assert bke.valid_page_count == len(bke.valid_pages)
        assert bke.invalid_page_count == len(bke.invalid_pages)
        assert bke.free_page_count + bke.valid_page_count + bke.invalid_page_count == PAGE_PER_BLOCK
        if block_id in plane_bke.free_block_pool:
            assert bke.free_page_count == PAGE_PER_BLOCK
            assert bke.valid_page_count == 0
            assert bke.invalid_page_count == 0
            assert bke.write_frontier == 0
        total_free += bke.free_page_count
        total_valid += bke.valid_page_count
        total_invalid += bke.invalid_page_count
    assert plane_bke.free_page_count == total_free
    assert plane_bke.valid_page_count == total_valid
    assert plane_bke.invalid_page_count == total_invalid
    assert total_free + total_valid + total_invalid == PAGE_PER_BLOCK * bm.block_no_per_plane


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
    plane_bke.block_entries[3].invalid_page_count = 8
    plane_bke.block_entries[4].invalid_page_count = 8
    plane_bke.block_entries[5].invalid_page_count = 6

    plane_bke.gc_wl_barrier_blocks.add(2)
    owner = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=44,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=0),
    )
    waiter = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=44,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=3, page=0),
    )
    bm._set_barrier(owner)
    bm._set_barrier(waiter)

    assert unit._pick_gc_victim_block(plane_addr) == 5


def test_gc_victim_selection_excludes_free_and_erase_barrier_blocks():
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.write_frontier_block = 1
    plane_bke.free_block_pool = {2}
    plane_bke.gc_erase_barrier_block_id = 3

    plane_bke.block_entries[1].invalid_page_count = 12
    plane_bke.block_entries[2].invalid_page_count = 10
    plane_bke.block_entries[3].invalid_page_count = 9
    plane_bke.block_entries[4].invalid_page_count = 5

    assert unit._pick_gc_victim_block(plane_addr) == 4


def test_gc_victim_selection_uses_greedy_invalid_page_priority():
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.free_block_pool = set()
    plane_bke.write_frontier_block = 1
    plane_bke.block_entries[2].invalid_page_count = 4
    plane_bke.block_entries[2].wl_level = 0
    plane_bke.block_entries[3].invalid_page_count = 7
    plane_bke.block_entries[3].wl_level = 9
    plane_bke.block_entries[4].invalid_page_count = 6
    plane_bke.block_entries[4].wl_level = 0

    assert unit._pick_gc_victim_block(plane_addr) == 3


def test_gc_victim_selection_tie_breaks_by_wear_then_block_id():
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.free_block_pool = set()
    plane_bke.write_frontier_block = 1
    plane_bke.block_entries[2].invalid_page_count = 5
    plane_bke.block_entries[2].wl_level = 3
    plane_bke.block_entries[3].invalid_page_count = 5
    plane_bke.block_entries[3].wl_level = 1
    plane_bke.block_entries[4].invalid_page_count = 5
    plane_bke.block_entries[4].wl_level = 1

    assert unit._pick_gc_victim_block(plane_addr) == 3


def test_gc_victim_selection_d_choices_uses_sample_min_valid_pages():
    bm, unit, _ = _make_gc_wl_fixture()
    unit.apply_runtime_config(
        RuntimeConfig(gc_victim_policy="rga", gc_d_choices=99, gc_random_seed=7)
    )
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)

    candidate_blocks = {2, 3, 4}
    plane_bke.write_frontier_block = 1
    plane_bke.free_block_pool = set(range(bm.block_no_per_plane)) - candidate_blocks
    for block_id, valid_count, invalid_count in (
        (2, 6, 2),
        (3, 1, 1),
        (4, 2, 6),
    ):
        bke = plane_bke.block_entries[block_id]
        bke.write_frontier = PAGE_PER_BLOCK
        bke.free_page_count = 0
        bke.valid_page_count = valid_count
        bke.invalid_page_count = invalid_count

    assert unit._pick_gc_victim_block(plane_addr) == 3


def test_trigger_gc_skips_without_recording_when_no_safe_invalid_victim(capsys):
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture()
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)

        plane_bke.write_frontier_block = 1
        plane_bke.free_block_pool = {2, 3}
        plane_bke.block_entries[1].invalid_page_count = 8
        plane_bke.block_entries[2].invalid_page_count = 7
        plane_bke.block_entries[3].invalid_page_count = 6

        unit._trigger_gc(plane_addr)

        maintenance = recorder.export()["meta"]["maintenance"]
        capsys.readouterr()
        assert fake_tsu.submitted == []
        assert maintenance["gc_count"] == 0
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_trigger_gc_skips_when_safe_blocks_have_no_invalid_pages(capsys):
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture()
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)

        plane_bke.write_frontier_block = 1
        plane_bke.free_block_pool = set()
        plane_bke.block_entries[2].valid_page_count = 4
        plane_bke.block_entries[3].valid_page_count = 7
        plane_bke.block_entries[4].valid_page_count = 1

        unit._trigger_gc(plane_addr)

        maintenance = recorder.export()["meta"]["maintenance"]
        capsys.readouterr()
        assert fake_tsu.submitted == []
        assert maintenance["gc_count"] == 0
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_trigger_gc_submits_complete_relocation_chain_to_safe_destination():
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    source_block = 2
    unsafe_free_block = 5
    dest_block = 6

    plane_bke.write_frontier_block = 1
    plane_bke.free_block_pool = {unsafe_free_block, dest_block}
    plane_bke.block_entries[source_block].valid_pages = {0, 2}
    plane_bke.block_entries[source_block].valid_page_count = 2
    plane_bke.block_entries[source_block].invalid_page_count = 3
    plane_bke.block_entries[unsafe_free_block].valid_page_count = 1

    for page_idx, lpa in ((0, 101), (2, 202)):
        pd = fake_tsu.phy._storage[0][0][0][0][source_block][page_idx]
        pd.function = PageType.USER
        pd.lpa = lpa
        pd.valid_bitmap = [1] * SECTOR_PER_PAGE

    unit._trigger_gc(plane_addr)

    submitted = fake_tsu.submitted
    submitted_types = [tr.type for tr in submitted]
    gc_reads = [tr for tr in submitted if tr.type == TransactionType.GC_READ]
    gc_writes = [tr for tr in submitted if tr.type == TransactionType.GC_WRITE]
    gc_erase = next(tr for tr in submitted if tr.type == TransactionType.GC_ERASE)

    assert submitted_types == [
        TransactionType.GC_READ,
        TransactionType.GC_WRITE,
        TransactionType.GC_READ,
        TransactionType.GC_WRITE,
        TransactionType.GC_ERASE,
    ]
    assert fake_tsu.prepared == 1
    assert fake_tsu.scheduled == 1
    assert [tr.lpa for tr in gc_reads] == [101, 202]
    assert [tr.lpa for tr in gc_writes] == [101, 202]
    assert [tr.address.sub_plane for tr in gc_writes] == [dest_block, dest_block]
    assert [tr.address.page for tr in gc_writes] == [0, 1]
    assert [tr.gc_old_address.page for tr in gc_writes] == [0, 2]
    assert gc_reads[0].required_by_transactions == [gc_writes[0]]
    assert gc_reads[1].required_by_transactions == [gc_writes[1]]
    assert gc_writes[0].rely_on_transactions == [gc_reads[0]]
    assert gc_writes[1].rely_on_transactions == [gc_reads[1]]
    assert gc_writes[0].required_by_transactions == [gc_erase]
    assert gc_writes[1].required_by_transactions == [gc_erase]
    assert gc_erase.rely_on_transactions == gc_writes
    assert gc_erase.address.sub_plane == source_block
    assert plane_bke.gc_erase_barrier_block_id == source_block
    assert plane_bke.gc_wl_barrier_blocks == {source_block, dest_block}
    assert dest_block not in plane_bke.free_block_pool


def test_trigger_gc_with_no_valid_pages_submits_erase_only_chain():
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2

        plane_bke.write_frontier_block = 1
        plane_bke.free_block_pool = set()
        plane_bke.block_entries[source_block].valid_pages = set()
        plane_bke.block_entries[source_block].valid_page_count = 0
        plane_bke.block_entries[source_block].invalid_page_count = 5

        unit._trigger_gc(plane_addr)

        submitted = fake_tsu.submitted
        maintenance = recorder.export()["meta"]["maintenance"]
        assert [tr.type for tr in submitted] == [TransactionType.GC_ERASE]
        assert submitted[0].address.sub_plane == source_block
        assert submitted[0].rely_on_transactions == []
        assert fake_tsu.prepared == 1
        assert fake_tsu.scheduled == 1
        assert plane_bke.gc_erase_barrier_block_id == source_block
        assert plane_bke.gc_wl_barrier_blocks == {source_block}
        assert maintenance["gc_count"] == 1
        assert maintenance["gc_relocated_pages"] == 0
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_submit_relocation_chain_resolves_lpa_from_gmt_when_phy_lpa_is_missing():
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    source_block = 2
    dest_block = 6
    source_addr = FlashAddress(
        channel=0,
        chip=0,
        die=0,
        plane=0,
        sub_plane=source_block,
        page=3,
    )

    plane_bke.free_block_pool = {dest_block}
    plane_bke.block_entries[source_block].valid_pages = {source_addr.page}
    plane_bke.block_entries[source_block].valid_page_count = 1
    plane_bke.block_entries[source_block].invalid_page_count = 2
    fake_tsu.phy._storage[0][0][0][0][source_block][source_addr.page].lpa = INVALID_LPA
    unit.address_mapping_unit.gmt[303] = cmt_entry(address=source_addr, dirty=False)

    submitted = unit._submit_relocation_chain(
        plane_addr,
        source_block,
        dest_block,
        "gc",
    )

    gc_read = next(tr for tr in fake_tsu.submitted if tr.type == TransactionType.GC_READ)
    gc_write = next(tr for tr in fake_tsu.submitted if tr.type == TransactionType.GC_WRITE)
    assert submitted is True
    assert gc_read.lpa == 303
    assert gc_write.lpa == 303
    assert gc_write.gc_old_address == source_addr


def test_submit_relocation_chain_skips_when_destination_lacks_free_pages(capsys):
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2
        dest_block = 6

        plane_bke.free_block_pool = {dest_block}
        plane_bke.block_entries[source_block].valid_pages = {0, 1, 2}
        plane_bke.block_entries[source_block].valid_page_count = 3
        plane_bke.block_entries[source_block].invalid_page_count = 1
        dest_bke = plane_bke.block_entries[dest_block]
        dest_bke.free_page_count = 2
        dest_bke.write_frontier = PAGE_PER_BLOCK - 2
        original_write_frontier = dest_bke.write_frontier

        submitted = unit._submit_relocation_chain(
            plane_addr,
            source_block,
            dest_block,
            "gc",
        )

        maintenance = recorder.export()["meta"]["maintenance"]
        assert submitted is False
        capsys.readouterr()
        assert fake_tsu.prepared == 0
        assert fake_tsu.scheduled == 0
        assert fake_tsu.submitted == []
        assert plane_bke.gc_erase_barrier_block_id is None
        assert plane_bke.gc_wl_barrier_blocks == set()
        assert dest_bke.write_frontier == original_write_frontier
        assert maintenance["gc_count"] == 0
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_relocation_lpa_resolution_failure_does_not_record_gc_start():
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2
        dest_block = 6
        source_bke = plane_bke.block_entries[source_block]
        source_bke.valid_pages = {0}
        source_bke.valid_page_count = 1
        source_bke.invalid_page_count = 1
        plane_bke.free_block_pool = {dest_block}

        with pytest.raises(ValueError, match="cannot resolve LPA"):
            unit._submit_relocation_chain(
                plane_addr,
                source_block,
                dest_block,
                "gc",
            )

        maintenance = recorder.export()["meta"]["maintenance"]
        assert maintenance["gc_count"] == 0
        assert maintenance["gc_relocated_pages"] == 0
        assert maintenance["gc_erased_blocks"] == 0
        assert fake_tsu.prepared == 0
        assert fake_tsu.submitted == []
        assert plane_bke.gc_wl_barrier_blocks == set()
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_gc_write_complete_updates_cmt_mapping_and_page_state():
    bm, _, _, amu = _make_gc_mapping_fixture()
    lpa = 77
    old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=3)
    plane_addr = _plane_addr()
    dest_block = 6

    bm._mark_valid(old_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {dest_block}
    new_addr = bm.allocate_gc_write_page(plane_addr, dest_block)
    amu.cmt.cache[lpa] = cmt_entry(address=old_addr, dirty=False)
    amu.cmt.lru_list = [lpa]

    gc_write = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=new_addr,
        gc_old_address=old_addr,
    )

    amu.apply_gc_write_complete(gc_write)

    old_bke = bm.get_block_bke(old_addr)
    new_bke = bm.get_block_bke(new_addr)
    assert amu.cmt.cache[lpa].address == new_addr
    assert amu.cmt.cache[lpa].dirty is True
    assert old_addr.page not in old_bke.valid_pages
    assert old_addr.page in old_bke.invalid_pages
    assert new_addr.page in new_bke.valid_pages


def test_gc_write_complete_updates_gmt_mapping_when_cmt_misses():
    bm, _, _, amu = _make_gc_mapping_fixture()
    lpa = 88
    old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=3, page=4)
    plane_addr = _plane_addr()
    dest_block = 7

    bm._mark_valid(old_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {dest_block}
    new_addr = bm.allocate_gc_write_page(plane_addr, dest_block)
    amu.gmt[lpa] = cmt_entry(address=old_addr, dirty=False)

    gc_write = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=new_addr,
        gc_old_address=old_addr,
    )

    amu.apply_gc_write_complete(gc_write)

    old_bke = bm.get_block_bke(old_addr)
    new_bke = bm.get_block_bke(new_addr)
    assert amu.gmt[lpa].address == new_addr
    assert old_addr.page not in old_bke.valid_pages
    assert old_addr.page in old_bke.invalid_pages
    assert new_addr.page in new_bke.valid_pages


def test_stale_gc_write_complete_does_not_rewind_cmt_mapping():
    bm, _, _, amu = _make_gc_mapping_fixture()
    lpa = 99
    old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=5)
    newer_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=1)
    plane_addr = _plane_addr()
    stale_dest_block = 8

    bm._mark_valid(old_addr)
    bm._mark_invalid(old_addr)
    bm._mark_valid(newer_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {stale_dest_block}
    stale_gc_addr = bm.allocate_gc_write_page(plane_addr, stale_dest_block)
    amu.cmt.cache[lpa] = cmt_entry(address=newer_addr, dirty=True)
    amu.cmt.lru_list = [lpa]

    gc_write = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=stale_gc_addr,
        gc_old_address=old_addr,
    )

    amu.apply_gc_write_complete(gc_write)

    stale_bke = bm.get_block_bke(stale_gc_addr)
    assert amu.cmt.cache[lpa].address == newer_addr
    assert stale_gc_addr.page not in stale_bke.valid_pages
    assert stale_gc_addr.page in stale_bke.invalid_pages


def test_gc_write_complete_promotes_fixed_metadata_mapping_into_dirty_cmt():
    bm, _, fake_tsu, amu = _make_gc_mapping_fixture()
    lpa = 111
    mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
    old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=3, page=6)
    plane_addr = _plane_addr()
    dest_block = 9
    mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
    page = fake_tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]

    page.function = PageType.MAPPING
    page.lpa = INVALID_LPA
    page.mvpn = mvpn
    page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
    page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
    page.valid_bitmap[lpa % LPA_NO_PER_MAPPING_PAGE] = 1
    page.data[lpa % LPA_NO_PER_MAPPING_PAGE] = utils.translate_address_to_ppa(old_addr)
    amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

    bm._mark_valid(old_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {dest_block}
    new_addr = bm.allocate_gc_write_page(plane_addr, dest_block)

    gc_write = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=new_addr,
        gc_old_address=old_addr,
    )

    amu.apply_gc_write_complete(gc_write)

    old_bke = bm.get_block_bke(old_addr)
    new_bke = bm.get_block_bke(new_addr)
    assert amu.cmt.cache[lpa].address == new_addr
    assert amu.cmt.cache[lpa].dirty is True
    assert old_addr.page not in old_bke.valid_pages
    assert old_addr.page in old_bke.invalid_pages
    assert new_addr.page in new_bke.valid_pages


def test_gc_mapping_and_phy_storage_stay_aligned_through_source_erase():
    bm, unit, fake_tsu, amu = _make_gc_mapping_fixture()
    unit.static_wl_wear_gap_threshold = 10_000
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    source_block = 2
    dest_block = 6
    lpa = 112

    plane_bke.write_frontier_block = source_block
    plane_bke.free_block_pool = {source_block, dest_block}
    source_addr = bm.get_write_frontier(plane_addr)
    source_write = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=lpa,
        address=source_addr,
        bitmap=[1] * SECTOR_PER_PAGE,
        payload=[1] * SECTOR_PER_PAGE,
    )
    fake_tsu.phy._write_to_storage(source_write)
    bm._mark_valid(source_addr)
    amu.cmt.cache[lpa] = cmt_entry(address=source_addr, dirty=False)
    amu.cmt.lru_list = [lpa]

    new_addr = bm.allocate_gc_write_page(plane_addr, dest_block)
    gc_write = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=new_addr,
        bitmap=[1] * SECTOR_PER_PAGE,
        payload=[2] * SECTOR_PER_PAGE,
        gc_old_address=source_addr,
    )
    fake_tsu.phy._write_to_storage(gc_write)
    amu.apply_gc_write_complete(gc_write)

    source_page = fake_tsu.phy._storage[0][0][0][0][source_block][source_addr.page]
    dest_page = fake_tsu.phy._storage[0][0][0][0][dest_block][new_addr.page]
    assert amu.cmt.cache[lpa].address == new_addr
    assert source_page.function == PageType.USER
    assert dest_page.function == PageType.USER
    assert dest_page.lpa == lpa
    assert dest_page.data == [2] * SECTOR_PER_PAGE

    bm.finalize_gc_erase(source_addr)

    erased_source_page = fake_tsu.phy._storage[0][0][0][0][source_block][source_addr.page]
    assert erased_source_page.function is None
    assert amu.cmt.cache[lpa].address == new_addr
    assert dest_page.function == PageType.USER
    assert dest_page.lpa == lpa
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_user_write_completion_preserves_block_bookkeeping_conservation():
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    dest_block = 6
    plane_bke = bm.get_plane_bke(plane_addr)
    plane_bke.free_block_pool = {dest_block}
    user_addr = bm.get_write_frontier(plane_addr)
    tr = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=123,
        address=user_addr,
    )

    bm._on_transaction_serviced(tr)

    bke = bm.get_block_bke(user_addr)
    assert user_addr.page in bke.valid_pages
    assert user_addr.page not in bke.invalid_pages
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_gc_write_completion_preserves_block_bookkeeping_conservation():
    bm, _, _, amu = _make_gc_mapping_fixture()
    plane_addr = _plane_addr()
    source_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=3)
    dest_block = 7
    lpa = 124

    bm._mark_valid(source_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {dest_block}
    gc_addr = bm.allocate_gc_write_page(plane_addr, dest_block)
    amu.cmt.cache[lpa] = cmt_entry(address=source_addr, dirty=False)
    amu.cmt.lru_list = [lpa]
    tr = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=gc_addr,
        gc_old_address=source_addr,
    )

    bm._on_transaction_serviced(tr)

    source_bke = bm.get_block_bke(source_addr)
    dest_bke = bm.get_block_bke(gc_addr)
    assert source_addr.page in source_bke.invalid_pages
    assert gc_addr.page in dest_bke.valid_pages
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_gc_erase_completion_resets_block_and_preserves_plane_bookkeeping():
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    unit.static_wl_wear_gap_threshold = 10_000
    plane_addr = _plane_addr()
    erase_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=0)
    bke = bm.get_block_bke(erase_addr)
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.free_block_pool.discard(erase_addr.sub_plane)
    bke.write_frontier = PAGE_PER_BLOCK
    bke.free_page_count = 0
    bke.valid_pages = {0, 1}
    bke.invalid_pages = {2, 3, 4, 5, 6, 7}
    bke.valid_page_count = 2
    bke.invalid_page_count = 6
    plane_bke.free_page_count -= PAGE_PER_BLOCK
    plane_bke.valid_page_count += 2
    plane_bke.invalid_page_count += 6
    plane_bke.gc_erase_barrier_block_id = erase_addr.sub_plane
    plane_bke.gc_wl_barrier_blocks = {erase_addr.sub_plane}

    bm.finalize_gc_erase(erase_addr)

    assert fake_tsu.submitted == []
    assert erase_addr.sub_plane in plane_bke.free_block_pool
    assert bke.free_page_count == PAGE_PER_BLOCK
    assert bke.valid_page_count == 0
    assert bke.invalid_page_count == 0
    assert bke.valid_pages == set()
    assert bke.invalid_pages == set()
    assert bke.write_frontier == 0
    assert plane_bke.gc_erase_barrier_block_id is None
    assert plane_bke.gc_wl_barrier_blocks == set()
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_mark_invalid_is_idempotent_for_block_bookkeeping():
    bm, _, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    bm.get_plane_bke(plane_addr).free_block_pool = {2}
    addr = bm.get_write_frontier(plane_addr)

    bm._mark_valid(addr)
    bm._mark_invalid(addr)
    snapshot = (
        bm.get_block_bke(addr).valid_page_count,
        bm.get_block_bke(addr).invalid_page_count,
        bm.get_plane_bke(plane_addr).valid_page_count,
        bm.get_plane_bke(plane_addr).invalid_page_count,
    )
    bm._mark_invalid(addr)

    assert snapshot == (
        bm.get_block_bke(addr).valid_page_count,
        bm.get_block_bke(addr).invalid_page_count,
        bm.get_plane_bke(plane_addr).valid_page_count,
        bm.get_plane_bke(plane_addr).invalid_page_count,
    )
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_stale_gc_write_completion_invalidates_reserved_target_without_count_drift():
    bm, _, _, amu = _make_gc_mapping_fixture()
    plane_addr = _plane_addr()
    old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=3)
    newer_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=1)
    stale_dest_block = 8
    lpa = 125

    bm._mark_valid(old_addr)
    bm._mark_invalid(old_addr)
    bm._mark_valid(newer_addr)
    bm.get_plane_bke(plane_addr).free_block_pool = {stale_dest_block}
    stale_gc_addr = bm.allocate_gc_write_page(plane_addr, stale_dest_block)
    amu.cmt.cache[lpa] = cmt_entry(address=newer_addr, dirty=True)
    amu.cmt.lru_list = [lpa]
    tr = Transaction(
        source_req=None,
        type=TransactionType.GC_WRITE,
        lpa=lpa,
        address=stale_gc_addr,
        gc_old_address=old_addr,
    )

    amu.apply_gc_write_complete(tr)

    stale_bke = bm.get_block_bke(stale_gc_addr)
    assert stale_gc_addr.page not in stale_bke.valid_pages
    assert stale_gc_addr.page in stale_bke.invalid_pages
    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_gc_erase_completion_on_partially_used_block_preserves_plane_bookkeeping():
    bm, unit, _ = _make_gc_wl_fixture(with_phy=True)
    unit.static_wl_wear_gap_threshold = 10_000
    plane_addr = _plane_addr()
    erase_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=0)
    bke = bm.get_block_bke(erase_addr)
    plane_bke = bm.get_plane_bke(plane_addr)

    plane_bke.free_block_pool.discard(erase_addr.sub_plane)
    bke.write_frontier = 4
    bke.free_page_count = PAGE_PER_BLOCK - 4
    bke.valid_pages = {0, 1}
    bke.invalid_pages = {2, 3}
    bke.valid_page_count = 2
    bke.invalid_page_count = 2
    plane_bke.free_page_count -= 4
    plane_bke.valid_page_count += 2
    plane_bke.invalid_page_count += 2
    plane_bke.gc_erase_barrier_block_id = erase_addr.sub_plane
    plane_bke.gc_wl_barrier_blocks = {erase_addr.sub_plane}

    bm.finalize_gc_erase(erase_addr)

    _assert_plane_bookkeeping_consistent(bm, plane_addr)


def test_check_gc_triggers_at_low_watermark_and_records_snapshot(monkeypatch):
    """GC starts at the inclusive low-watermark boundary and reports that state."""
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, _ = _make_gc_wl_fixture()
        unit.gc_low_watermark = 3
        plane_addr = _plane_addr()
        bm.get_plane_bke(plane_addr).free_block_pool = {10, 11, 12}
        triggered: list[FlashAddress] = []
        monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

        unit.check_gc()

        maintenance = recorder.export()["meta"]["maintenance"]
        assert triggered == [plane_addr]
        assert maintenance["min_free_pool"] == 3
        assert maintenance["planes"]["ch0.chip0.die0.plane0"]["min_free_pool"] == 3
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_check_gc_does_not_trigger_above_low_watermark(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.gc_low_watermark = 3
    plane_addr = _plane_addr()
    bm.get_plane_bke(plane_addr).free_block_pool = {10, 11, 12, 13}
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    unit.check_gc()

    assert triggered == []


def test_check_gc_triggers_below_low_watermark_and_records_actual_pool(monkeypatch):
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, _ = _make_gc_wl_fixture()
        unit.gc_low_watermark = 3
        plane_addr = _plane_addr()
        bm.get_plane_bke(plane_addr).free_block_pool = {10, 11}
        triggered: list[FlashAddress] = []
        monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

        unit.check_gc()

        maintenance = recorder.export()["meta"]["maintenance"]
        assert triggered == [plane_addr]
        assert maintenance["min_free_pool"] == 2
        assert maintenance["planes"]["ch0.chip0.die0.plane0"]["min_free_pool"] == 2
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_check_gc_uses_runtime_configured_low_watermark(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.apply_runtime_config(
        RuntimeConfig(gc_low_watermark=5, stop_servicing_writes_threshold=1)
    )
    plane_addr = _plane_addr()
    bm.get_plane_bke(plane_addr).free_block_pool = {10, 11, 12, 13, 14}
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    unit.check_gc()

    assert triggered == [plane_addr]
    assert unit.gc_low_watermark == 5


def test_check_gc_uses_strict_ratio_threshold_when_configured(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.apply_runtime_config(
        RuntimeConfig(gc_low_watermark=999, gc_exec_threshold=0.05)
    )
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    threshold = unit._gc_trigger_threshold_blocks()
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    plane_bke.free_block_pool = set(range(threshold))
    unit.check_gc()
    assert triggered == []

    plane_bke.free_block_pool = set(range(threshold - 1))
    unit.check_gc()
    assert triggered == [plane_addr]


def test_check_gc_attempts_trigger_at_zero_watermark_with_empty_pool(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.apply_runtime_config(
        RuntimeConfig(gc_low_watermark=0, stop_servicing_writes_threshold=0)
    )
    plane_addr = _plane_addr()
    bm.get_plane_bke(plane_addr).free_block_pool = set()
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    unit.check_gc()

    assert triggered == [plane_addr]


def test_check_gc_skips_low_pool_when_gc_wl_barrier_is_active(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.gc_low_watermark = 3
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    plane_bke.free_block_pool = {10, 11}
    plane_bke.gc_wl_barrier_blocks.add(3)
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    unit.check_gc()

    assert triggered == []


def test_on_erase_complete_triggers_static_wl_and_blocks_conflicting_writes():
    """Static WL submits a relocation chain and installs block barriers for conflicts."""
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    unit.gc_low_watermark = 0
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

    blocked_source_write = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=1000,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=source_block, page=2),
    )
    other_plane_write = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=1001,
        address=FlashAddress(channel=0, chip=0, die=1, plane=0, sub_plane=dest_block, page=0),
    )
    assert tsu._transaction_blocked_by_barrier(blocked_source_write) is True
    assert tsu._transaction_blocked_by_barrier(other_plane_write) is False


def test_static_wl_trigger_uses_inclusive_wear_gap_and_skips_active_maintenance(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    for bke in plane_bke.block_entries:
        bke.wl_level = 4
    plane_bke.block_entries[2].wl_level = 1
    unit.static_wl_wear_gap_threshold = 3
    triggered: list[FlashAddress] = []
    monkeypatch.setattr(unit, "_trigger_static_wl", lambda addr: triggered.append(addr))

    unit.on_erase_complete(plane_addr)
    assert triggered == [plane_addr]

    triggered.clear()
    unit.static_wl_wear_gap_threshold = 4
    unit.on_erase_complete(plane_addr)
    assert triggered == []

    unit.static_wl_wear_gap_threshold = 3
    plane_bke.gc_wl_barrier_blocks.add(7)
    unit.on_erase_complete(plane_addr)
    assert triggered == []


def test_gc_erase_completion_checks_static_wl_after_releasing_old_barriers(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    unit.gc_low_watermark = 0
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    erased_block = 2
    erase_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=erased_block, page=0)
    erased_bke = plane_bke.block_entries[erased_block]
    erased_bke.wl_level = 3
    plane_bke.gc_erase_barrier_block_id = erased_block
    plane_bke.gc_wl_barrier_blocks = {erased_block, 6}
    unit.static_wl_wear_gap_threshold = 1
    observed: list[tuple[FlashAddress, int | None, set[int], bool]] = []

    def record_trigger(addr: FlashAddress) -> bool:
        observed.append(
            (
                addr,
                plane_bke.gc_erase_barrier_block_id,
                set(plane_bke.gc_wl_barrier_blocks),
                erased_block in plane_bke.free_block_pool,
            )
        )
        return True

    monkeypatch.setattr(unit, "_trigger_static_wl", record_trigger)

    bm.finalize_gc_erase(erase_addr)

    assert erased_bke.wl_level == 4
    assert observed == [(plane_addr, None, set(), True)]


def test_static_wl_erase_does_not_recursively_trigger_static_wl(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    for bke in plane_bke.block_entries:
        bke.wl_level = 4
    plane_bke.block_entries[2].wl_level = 0
    triggered = []
    monkeypatch.setattr(unit, "_trigger_static_wl", lambda addr: triggered.append(addr))

    unit.on_erase_complete(plane_addr, reason="static-wl")

    assert triggered == []


def test_static_wl_yields_to_waiting_writes_and_low_free_pool(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    for bke in plane_bke.block_entries:
        bke.wl_level = 4
    plane_bke.block_entries[2].wl_level = 0
    unit.gc_low_watermark = 3
    triggered = []
    monkeypatch.setattr(unit, "_trigger_static_wl", lambda addr: triggered.append(addr))

    plane_bke.free_block_pool = {5, 6, 7}
    unit.on_erase_complete(plane_addr)
    assert triggered == []

    plane_bke.free_block_pool.add(8)
    waiting = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=99,
        address=plane_addr,
    )
    bm.waiting_writes[bm._plane_key(plane_addr)] = [waiting]
    unit.on_erase_complete(plane_addr)
    assert triggered == []

    bm.waiting_writes.clear()
    unit.on_erase_complete(plane_addr)
    assert triggered == [plane_addr]


def test_erase_retries_waiting_write_when_no_gc_victim_can_progress(monkeypatch):
    bm, unit, fake_tsu, _ = _make_gc_mapping_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    erase_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=0)
    plane_bke.free_block_pool = set()
    bm.STOP_SERVICING_WRITES_THRESHOLD = 1
    unit.gc_low_watermark = 3
    unit.static_wl_wear_gap_threshold = 10_000
    waiting = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=1234,
        address=plane_addr,
    )
    bm.waiting_writes[bm._plane_key(plane_addr)] = [waiting]
    triggered = []
    monkeypatch.setattr(unit, "_trigger_gc", lambda addr: triggered.append(addr))

    bm.finalize_gc_erase(erase_addr)

    assert bm._plane_key(plane_addr) not in bm.waiting_writes
    assert fake_tsu.submitted == [waiting]
    assert triggered == []


def test_static_wl_source_selection_excludes_every_unsafe_block_class():
    bm, unit, _ = _make_gc_wl_fixture()
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    plane_bke.write_frontier_block = 1
    plane_bke.gc_erase_barrier_block_id = 2
    plane_bke.gc_wl_barrier_blocks = {3}
    plane_bke.free_block_pool = {5}

    for block_id in range(1, 7):
        bke = plane_bke.block_entries[block_id]
        bke.valid_page_count = 1
        bke.wl_level = block_id

    inflight_write = Transaction(
        source_req=None,
        type=TransactionType.USER_WRITE,
        lpa=404,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=0),
    )
    bm._set_barrier(inflight_write)

    assert unit._pick_static_wl_source_block(plane_addr) == 6


def test_static_wl_destination_is_highest_wear_eligible_free_block():
    bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
    plane_addr = _plane_addr()
    plane_bke = bm.get_plane_bke(plane_addr)
    source_block = 2
    source_bke = plane_bke.block_entries[source_block]
    source_bke.wl_level = 0
    source_bke.valid_pages = {0}
    source_bke.valid_page_count = 1
    source_bke.free_page_count = PAGE_PER_BLOCK - 1
    plane_bke.free_block_pool = {4, 5, 6, 7}

    plane_bke.block_entries[4].wl_level = 9
    plane_bke.block_entries[4].valid_page_count = 1
    plane_bke.block_entries[5].wl_level = 8
    plane_bke.block_entries[5].write_frontier = 1
    plane_bke.block_entries[6].wl_level = 7
    plane_bke.gc_wl_barrier_blocks = {6}
    plane_bke.block_entries[7].wl_level = 6

    source_page = fake_tsu.phy._storage[0][0][0][0][source_block][0]
    source_page.lpa = 707
    source_page.valid_bitmap = [1] * SECTOR_PER_PAGE

    assert unit._trigger_static_wl(plane_addr) is True

    gc_write = next(tr for tr in fake_tsu.submitted if tr.type == TransactionType.GC_WRITE)
    assert gc_write.address.sub_plane == 7
    assert plane_bke.gc_wl_barrier_blocks == {source_block, 7}


def test_static_wl_without_threshold_qualified_safe_destination_has_no_side_effects():
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2
        source_bke = plane_bke.block_entries[source_block]
        source_bke.wl_level = 5
        source_bke.valid_pages = {0}
        source_bke.valid_page_count = 1
        plane_bke.free_block_pool = {6, 7}
        unit.static_wl_wear_gap_threshold = 2
        plane_bke.block_entries[6].wl_level = 6
        plane_bke.block_entries[7].wl_level = 9
        plane_bke.block_entries[7].invalid_page_count = 1

        assert unit._trigger_static_wl(plane_addr) is False

        maintenance = recorder.export()["meta"]["maintenance"]
        assert fake_tsu.prepared == 0
        assert fake_tsu.submitted == []
        assert fake_tsu.scheduled == 0
        assert plane_bke.gc_wl_barrier_blocks == set()
        assert maintenance["static_wl_count"] == 0
        assert maintenance["gc_relocated_pages"] == 0
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_erase_retries_waiting_writes_before_static_wl(monkeypatch):
    bm, unit, _ = _make_gc_wl_fixture()
    erase_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=0)
    calls = []
    monkeypatch.setattr(bm, "_retry_waiting_writes", lambda addr: calls.append("retry") or 0)
    monkeypatch.setattr(
        unit,
        "on_erase_complete",
        lambda addr, **kwargs: calls.append("static-wl-check"),
    )

    bm.finalize_gc_erase(erase_addr)

    assert calls == ["retry", "static-wl-check"]


def test_static_wl_completion_updates_wear_and_maintenance_report():
    recorder = RequestLatencyRecorder()
    SET_REQUEST_LATENCY_RECORDER(recorder)
    try:
        bm, unit, fake_tsu = _make_gc_wl_fixture(with_phy=True)
        plane_addr = _plane_addr()
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2
        dest_block = 6
        for bke in plane_bke.block_entries:
            bke.wl_level = 5

        source_bke = plane_bke.block_entries[source_block]
        source_bke.wl_level = 0
        source_bke.valid_pages = {0}
        source_bke.valid_page_count = 1
        source_bke.free_page_count = PAGE_PER_BLOCK - 1
        plane_bke.free_page_count -= 1
        plane_bke.valid_page_count += 1
        plane_bke.free_block_pool = {dest_block}

        source_page = fake_tsu.phy._storage[0][0][0][0][source_block][0]
        source_page.lpa = 808
        source_page.valid_bitmap = [1] * SECTOR_PER_PAGE

        assert unit._trigger_static_wl(plane_addr) is True
        erase = next(tr for tr in fake_tsu.submitted if tr.type == TransactionType.GC_ERASE)
        unit.static_wl_wear_gap_threshold = 10_000
        bm.finalize_gc_erase(erase.address)

        maintenance = recorder.export()["meta"]["maintenance"]
        expected_skew = max(bke.wl_level for bke in plane_bke.block_entries) - min(
            bke.wl_level for bke in plane_bke.block_entries
        )
        assert source_bke.wl_level == 1
        assert expected_skew == 4
        assert maintenance["static_wl_count"] == 1
        assert maintenance["gc_relocated_pages"] == 1
        assert maintenance["gc_erased_blocks"] == 1
        assert maintenance["max_wear_skew"] == expected_skew
        assert maintenance["planes"]["ch0.chip0.die0.plane0"]["max_wear_skew"] == expected_skew
    finally:
        SET_REQUEST_LATENCY_RECORDER(None)


def test_mapping_write_dependency_read_bypasses_its_own_mvpn_barrier():
    bm, _, _ = _make_gc_wl_fixture()
    tsu = TSU()
    tsu.block_manager = bm

    mapping_write = Transaction(
        source_req=None,
        type=TransactionType.MAPPING_WRITE,
        mvpn=7,
        address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=1, page=0),
    )
    dependency_read = Transaction(
        source_req=None,
        type=TransactionType.MAPPING_READ,
        mvpn=7,
        address=mapping_write.address,
    )
    mapping_write.rely_on_transactions.append(dependency_read)
    dependency_read.required_by_transactions.append(mapping_write)
    bm._set_barrier(mapping_write)

    unrelated_mapping_read = Transaction(
        source_req=None,
        type=TransactionType.MAPPING_READ,
        mvpn=7,
        address=mapping_write.address,
    )

    assert tsu._transaction_blocked_by_barrier(dependency_read) is False
    assert tsu._transaction_blocked_by_barrier(unrelated_mapping_read) is True


def test_tsu_batch_dispatch_preserves_same_plane_index_across_dies():
    bm, _, _ = _make_gc_wl_fixture()
    dispatched = []

    class RecordingPHY:
        def send_command_to_chip(self, chip_id, transactions, suspension_required):
            dispatched.append((chip_id, list(transactions), suspension_required))

    tsu = TSU()
    tsu.block_manager = bm
    tsu.phy = RecordingPHY()
    transactions = [
        Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=die,
            address=FlashAddress(
                channel=0,
                chip=0,
                die=die,
                plane=0,
                sub_plane=1,
                page=2,
            ),
        )
        for die in (0, 1)
    ]
    queue = list(transactions)

    assert tsu.issue_command((0, 0), queue, None, False) is True

    assert queue == []
    assert len(dispatched) == 2
    assert [batch[1] for batch in dispatched] == [[transactions[0]], [transactions[1]]]
    assert all(batch[0] == (0, 0) for batch in dispatched)
    assert all(batch[2] is False for batch in dispatched)
