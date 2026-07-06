"""Regression coverage for GC/write-path runtime policy."""

from types import SimpleNamespace
import unittest

from flash_sim.FTL import Address_Mapping_Unit, Block_Manager
from flash_sim.PHY import PHY, PageType
from flash_sim import utils
from flash_sim.common import (
    FlashAddress,
    GTDEntry,
    INVALID_DATA,
    INVALID_LPA,
    INVALID_MVPN,
    INVALID_PPA,
    LPA_NO_PER_MAPPING_PAGE,
    PAGE_PER_BLOCK,
    Request,
    RequestFailure,
    RequestType,
    Transaction,
    TransactionType,
    cmt_entry,
)
from flash_sim.config import FlashConfig, RuntimeConfig
from flash_sim.request_latency_report import RequestLatencyRecorder


class _FakeTSU:
    def __init__(self, phy=None):
        self.prepared = 0
        self.scheduled = 0
        self.submitted: list[Transaction] = []
        self.phy = phy

    def Prepare_trans_submission(self):
        self.prepared += 1

    def Submit_trans(self, tr: Transaction):
        self.submitted.append(tr)

    def Schedule(self):
        self.scheduled += 1


def _make_amu_fixture(with_phy: bool = False):
    bm = Block_Manager()
    amu = Address_Mapping_Unit()
    tsu = _FakeTSU(phy=PHY() if with_phy else None)
    amu.block_manager = bm
    amu.tsu = tsu
    bm.gc_wl_unit = SimpleNamespace(
        address_mapping_unit=amu,
        tsu=tsu,
        gc_low_watermark=3,
        check_gc=lambda: None,
        check_gc_for_plane=lambda addr: None,
        can_trigger_gc=lambda addr: True,
        _trigger_gc=lambda addr: None,
        on_erase_complete=lambda addr, **kwargs: None,
        select_wl_aware_free_block=lambda plane_addr, **kwargs: min(
            bm.get_plane_bke(plane_addr).free_block_pool
            - (kwargs.get("exclude_blocks") or set()),
            default=-1,
        ),
    )
    return amu, bm, tsu


class TestGCWritePathRuntime(unittest.TestCase):
    def test_runtime_config_parses_gc_policy_knobs(self):
        config = FlashConfig.from_dict(
            {
                "runtime": {
                    "gc_low_watermark": 5,
                    "gc_exec_threshold": 0.05,
                    "stop_servicing_writes_threshold": 2,
                    "gc_min_invalid_ratio": 0.25,
                    "gc_victim_policy": "RGA",
                    "gc_d_choices": 6,
                    "gc_random_seed": 99,
                    "static_wl_wear_gap_threshold": 7,
                }
            }
        )

        self.assertEqual(config.runtime.gc_low_watermark, 5)
        self.assertEqual(config.runtime.gc_exec_threshold, 0.05)
        self.assertEqual(config.runtime.stop_servicing_writes_threshold, 2)
        self.assertEqual(config.runtime.gc_min_invalid_ratio, 0.25)
        self.assertEqual(config.runtime.gc_victim_policy, "d-choices")
        self.assertEqual(config.runtime.gc_d_choices, 6)
        self.assertEqual(config.runtime.gc_random_seed, 99)
        self.assertEqual(config.runtime.static_wl_wear_gap_threshold, 7)

    def test_gmt_hit_write_sets_invalidation_target_at_submit_time(self):
        amu, bm, tsu = _make_amu_fixture()
        old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=9, page=3)
        bm._mark_valid(old_addr)
        amu.gmt[42] = cmt_entry(address=old_addr, dirty=True)
        plane_addr = amu.get_plane_address_for_lpa(42)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=42)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [tr])
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertNotEqual(tr.address, old_addr)
        self.assertTrue(amu.domains[0].cmt.is_cached(42))
        self.assertIn(42, bm.lpa_protected_book)
        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)

        bm._on_transaction_serviced(tr)

        old_bke = bm.get_block_bke(old_addr)
        self.assertNotIn(old_addr.page, old_bke.valid_pages)
        self.assertIn(old_addr.page, old_bke.invalid_pages)

    def test_first_write_backpressure_waits_without_allocating_ppa(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        plane_addr = amu.get_plane_address_for_lpa(0)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=0)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertEqual(tr.address.sub_plane, -1)
        self.assertFalse(amu.domains[0].cmt.is_cached(0))

    def test_waiting_flush_retry_updates_mapping_without_source_req(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        plane_addr = amu.get_plane_address_for_lpa(8)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}
        old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=2)
        bm._mark_valid(old_addr)
        amu.gmt[8] = cmt_entry(address=old_addr, dirty=True)

        tr = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=8,
            address=plane_addr,
            cache_flush_generated=True,
            report_origin_request_ids=["req-0008-write"],
        )
        tr._mapping_sq_id = 0
        bm.waiting_writes[bm._plane_key(plane_addr)] = [tr]

        bm._retry_waiting_writes(plane_addr)

        self.assertEqual(tsu.submitted, [tr])
        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertTrue(amu.domains[0].cmt.is_cached(8))

        bm._on_transaction_serviced(tr)

        old_bke = bm.get_block_bke(old_addr)
        self.assertNotIn(old_addr.page, old_bke.valid_pages)
        self.assertIn(old_addr.page, old_bke.invalid_pages)

    def test_waiting_first_write_re_resolves_as_cmt_overwrite_on_retry(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 10
        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]
        amu.translate_and_submit(req)

        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertEqual(tsu.submitted, [])

        old_addr = FlashAddress(
            channel=0,
            chip=0,
            die=0,
            plane=0,
            sub_plane=7,
            page=2,
        )
        bm._mark_valid(old_addr)
        amu.cmt.cache[lpa] = cmt_entry(address=old_addr, dirty=True)
        amu.cmt.lru_list = [lpa]

        bm._retry_waiting_writes(plane_addr)

        self.assertEqual(tsu.submitted, [tr])
        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertEqual(amu.cmt.cache[lpa].address, tr.address)
        old_bke = bm.get_block_bke(old_addr)
        self.assertNotIn(old_addr.page, old_bke.valid_pages)
        self.assertIn(old_addr.page, old_bke.invalid_pages)

    def test_waiting_retry_keeps_unmapped_write_queued_at_threshold(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 11
        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}
        frontier_bke = plane_bke.block_entries[plane_bke.write_frontier_block]
        initial_frontier = frontier_bke.write_frontier

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]
        amu.translate_and_submit(req)
        bm._retry_waiting_writes(plane_addr)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertEqual(frontier_bke.write_frontier, initial_frontier)
        self.assertFalse(amu.cmt.is_cached(lpa))
        self.assertIsNone(tr.invalidate_target)

    def test_consecutive_gc_erases_wake_waiting_writes_in_fifo_order(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        plane_addr = amu.get_plane_address_for_lpa(30)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}
        waiting = []

        for lpa in (30, 31):
            req = Request(type=RequestType.WRITE, sq_id=0)
            tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
            req.transaction_list = [tr]
            amu.translate_and_submit(req)
            waiting.append(tr)

        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], waiting)
        initial_schedule_count = tsu.scheduled
        bm.gc_wl_unit.can_trigger_gc = lambda addr: False

        erase_addr = FlashAddress(
            channel=plane_addr.channel,
            chip=plane_addr.chip,
            die=plane_addr.die,
            plane=plane_addr.plane,
            sub_plane=2,
            page=0,
        )
        erase_bke = bm.get_block_bke(erase_addr)
        erase_bke.write_frontier = PAGE_PER_BLOCK
        erase_bke.free_page_count = 0
        erase_bke.invalid_pages = set(range(PAGE_PER_BLOCK))
        erase_bke.invalid_page_count = PAGE_PER_BLOCK
        plane_bke.free_page_count -= PAGE_PER_BLOCK
        plane_bke.invalid_page_count += PAGE_PER_BLOCK

        bm.finalize_gc_erase(erase_addr)

        self.assertEqual(tsu.submitted, waiting)
        self.assertEqual(tsu.scheduled, initial_schedule_count + 1)

        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)
        self.assertEqual([tr.address.page for tr in waiting], [0, 1])
        for tr in waiting:
            self.assertEqual(amu.cmt.cache[tr.lpa].address, tr.address)
            self.assertIs(bm.lpa_protected_book[tr.lpa], tr)

    def test_waiting_retry_does_not_let_overwrite_overtake_blocked_head(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        plane_addr = amu.get_plane_address_for_lpa(32)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        head = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=32,
            address=plane_addr,
        )
        head._mapping_sq_id = 0
        overwrite = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=33,
            address=plane_addr,
        )
        overwrite._mapping_sq_id = 0
        old_addr = FlashAddress(
            channel=plane_addr.channel,
            chip=plane_addr.chip,
            die=plane_addr.die,
            plane=plane_addr.plane,
            sub_plane=6,
            page=1,
        )
        bm._mark_valid(old_addr)
        amu.cmt.cache[overwrite.lpa] = cmt_entry(address=old_addr, dirty=True)
        amu.cmt.lru_list = [overwrite.lpa]
        bm.waiting_writes[bm._plane_key(plane_addr)] = [head, overwrite]

        bm._retry_waiting_writes(plane_addr)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(
            bm.waiting_writes[bm._plane_key(plane_addr)],
            [head, overwrite],
        )
        self.assertEqual(amu.cmt.cache[overwrite.lpa].address, old_addr)
        self.assertIn(old_addr.page, bm.get_block_bke(old_addr).valid_pages)

    def test_gc_erase_does_not_wake_same_plane_index_on_another_die(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        pages_per_plane = bm.block_no_per_plane * bm.pages_per_block
        local_lpa = 40
        remote_lpa = pages_per_plane * amu.flash_geometry.planes_per_die
        local_plane = amu.get_plane_address_for_lpa(local_lpa)
        remote_plane = amu.get_plane_address_for_lpa(remote_lpa)
        self.assertEqual(local_plane.plane, remote_plane.plane)
        self.assertNotEqual(local_plane.die, remote_plane.die)

        local = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=local_lpa,
            address=local_plane,
        )
        local._mapping_sq_id = 0
        remote = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=remote_lpa,
            address=remote_plane,
        )
        remote._mapping_sq_id = 0
        remote_old_addr = FlashAddress(
            channel=remote_plane.channel,
            chip=remote_plane.chip,
            die=remote_plane.die,
            plane=remote_plane.plane,
            sub_plane=5,
            page=1,
        )
        bm._mark_valid(remote_old_addr)
        amu.cmt.cache[remote_lpa] = cmt_entry(address=remote_old_addr, dirty=True)
        amu.cmt.lru_list = [remote_lpa]

        local_bke = bm.get_plane_bke(local_plane)
        remote_bke = bm.get_plane_bke(remote_plane)
        local_bke.free_block_pool = {local_bke.write_frontier_block}
        remote_bke.free_block_pool = {remote_bke.write_frontier_block}
        bm.waiting_writes[bm._plane_key(local_plane)] = [local]
        bm.waiting_writes[bm._plane_key(remote_plane)] = [remote]
        bm.gc_wl_unit.can_trigger_gc = lambda addr: False

        erase_addr = FlashAddress(
            channel=local_plane.channel,
            chip=local_plane.chip,
            die=local_plane.die,
            plane=local_plane.plane,
            sub_plane=2,
            page=0,
        )
        bm.finalize_gc_erase(erase_addr)

        self.assertEqual(tsu.submitted, [local])
        self.assertEqual(bm.waiting_writes[bm._plane_key(remote_plane)], [remote])
        self.assertEqual(amu.cmt.cache[remote_lpa].address, remote_old_addr)
        self.assertIn(remote_old_addr.page, bm.get_block_bke(remote_old_addr).valid_pages)

    def test_overwrite_waits_when_no_physical_page_can_be_allocated(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 12
        old_addr = FlashAddress(
            channel=0,
            chip=0,
            die=0,
            plane=0,
            sub_plane=6,
            page=1,
        )
        bm._mark_valid(old_addr)
        amu.cmt.cache[lpa] = cmt_entry(address=old_addr, dirty=True)
        amu.cmt.lru_list = [lpa]

        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        frontier_bke = plane_bke.block_entries[plane_bke.write_frontier_block]
        frontier_bke.write_frontier = PAGE_PER_BLOCK
        plane_bke.free_block_pool.clear()

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]
        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertEqual(amu.cmt.cache[lpa].address, old_addr)
        self.assertIsNone(tr.invalidate_target)
        old_bke = bm.get_block_bke(old_addr)
        self.assertIn(old_addr.page, old_bke.valid_pages)
        self.assertNotIn(old_addr.page, old_bke.invalid_pages)

    def test_metadata_hit_write_sets_invalidation_target_and_forms_invalid_page(self):
        amu, bm, tsu = _make_amu_fixture(with_phy=True)
        lpa = 18
        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE

        old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=8, page=2)
        page.valid_bitmap[lpa % LPA_NO_PER_MAPPING_PAGE] = 1
        page.data[lpa % LPA_NO_PER_MAPPING_PAGE] = utils.translate_address_to_ppa(old_addr)
        bm._mark_valid(old_addr)

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [tr])
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertTrue(amu.domains[0].cmt.is_cached(lpa))

        bm._on_transaction_serviced(tr)
        block_bke = bm.get_block_bke(old_addr)
        self.assertIn(old_addr.page, block_bke.invalid_pages)
        self.assertNotIn(old_addr.page, block_bke.valid_pages)

    def test_metadata_slot_invalid_still_behaves_like_first_write(self):
        amu, bm, tsu = _make_amu_fixture(with_phy=True)
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 19
        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)
        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertIsNone(tr.invalidate_target)
        self.assertFalse(amu.domains[0].cmt.is_cached(lpa))

    def test_unmaterialized_gtd_entry_is_treated_as_unmapped(self):
        amu, bm, tsu = _make_amu_fixture(with_phy=True)
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 22
        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr, written=False)
        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        self.assertEqual(tsu.submitted, [])
        self.assertEqual(bm.waiting_writes[bm._plane_key(plane_addr)], [tr])
        self.assertIsNone(tr.invalidate_target)
        self.assertFalse(amu.domains[0].cmt.is_cached(lpa))

    def test_write_metadata_invalid_ppa_fails_explicitly(self):
        amu, _, tsu = _make_amu_fixture(with_phy=True)
        lpa = 20
        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        page.valid_bitmap[lpa % LPA_NO_PER_MAPPING_PAGE] = 1

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
        req.transaction_list = [tr]

        with self.assertRaisesRegex(RequestFailure, "invalid ppa"):
            amu.translate_and_submit(req)
        self.assertEqual(tsu.submitted, [])

    def test_waiting_retry_uses_fixed_metadata_overwrite_and_bypasses_threshold(self):
        amu, bm, tsu = _make_amu_fixture(with_phy=True)
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        lpa = 21
        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE

        old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=5, page=1)
        page.valid_bitmap[lpa % LPA_NO_PER_MAPPING_PAGE] = 1
        page.data[lpa % LPA_NO_PER_MAPPING_PAGE] = utils.translate_address_to_ppa(old_addr)
        bm._mark_valid(old_addr)

        plane_addr = amu.get_plane_address_for_lpa(lpa)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        tr = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=lpa,
            address=plane_addr,
            cache_flush_generated=True,
            report_origin_request_ids=["req-0021-write"],
        )
        tr._mapping_sq_id = 0
        bm.waiting_writes[bm._plane_key(plane_addr)] = [tr]

        bm._retry_waiting_writes(plane_addr)

        self.assertEqual(tsu.submitted, [tr])
        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertTrue(amu.domains[0].cmt.is_cached(lpa))

        bm._on_transaction_serviced(tr)

        old_bke = bm.get_block_bke(old_addr)
        self.assertNotIn(old_addr.page, old_bke.valid_pages)
        self.assertIn(old_addr.page, old_bke.invalid_pages)

    def test_mapping_write_sparse_merge_reads_only_old_valid_uncovered_slots(self):
        amu, _, tsu = _make_amu_fixture(with_phy=True)
        mvpn = 2
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        for slot in (1, 3, 7):
            page.valid_bitmap[slot] = 1
            page.data[slot] = 10_000 + slot

        updated_lpas = [mvpn * LPA_NO_PER_MAPPING_PAGE + 3, mvpn * LPA_NO_PER_MAPPING_PAGE + 5]
        amu.cmt.cache = {
            updated_lpas[0]: cmt_entry(
                address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=4),
                dirty=True,
            ),
            updated_lpas[1]: cmt_entry(
                address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=2, page=5),
                dirty=True,
            ),
        }
        amu.cmt.lru_list = list(updated_lpas)

        amu.generate_mapping_write_transaction(amu.cmt.cache, mvpn)

        read_tr = next(tr for tr in tsu.submitted if tr.type == TransactionType.MAPPING_READ)
        self.assertEqual(read_tr.bitmap[1], 1)
        self.assertEqual(read_tr.bitmap[7], 1)
        self.assertEqual(read_tr.bitmap[3], 0)
        self.assertEqual(read_tr.bitmap[5], 0)
        self.assertEqual(sum(read_tr.bitmap), 2)

    def test_mapping_write_sparse_merge_preserves_previous_valid_slots(self):
        amu, _, tsu = _make_amu_fixture(with_phy=True)
        mvpn = 4
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        old_slot = mvpn * LPA_NO_PER_MAPPING_PAGE + 1
        new_slot = mvpn * LPA_NO_PER_MAPPING_PAGE + 3
        old_slot_ppa = 11111
        old_other_slot_ppa = 17777

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        page.valid_bitmap[1] = 1
        page.data[1] = old_slot_ppa
        page.valid_bitmap[7] = 1
        page.data[7] = old_other_slot_ppa

        new_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=3, page=6)
        amu.cmt.cache = {new_slot: cmt_entry(address=new_addr, dirty=True)}
        amu.cmt.lru_list = [new_slot]

        amu.generate_mapping_write_transaction(amu.cmt.cache, mvpn)

        read_tr = next(tr for tr in tsu.submitted if tr.type == TransactionType.MAPPING_READ)
        write_tr = next(tr for tr in tsu.submitted if tr.type == TransactionType.MAPPING_WRITE)
        read_tr.response = tsu.phy._read_from_storage(read_tr)
        write_tr.get_response_from_transaction(read_tr)
        tsu.phy._write_to_storage(write_tr)

        merged = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        self.assertEqual(merged.valid_bitmap[1], 1)
        self.assertEqual(merged.data[1], old_slot_ppa)
        self.assertEqual(merged.valid_bitmap[7], 1)
        self.assertEqual(merged.data[7], old_other_slot_ppa)
        self.assertEqual(merged.valid_bitmap[3], 1)
        self.assertEqual(merged.data[3], utils.translate_address_to_ppa(new_addr))
        self.assertEqual(merged.valid_bitmap[2], 0)
        self.assertEqual(merged.data[2], INVALID_PPA)
        self.assertEqual(sum(merged.valid_bitmap), 3)

    def test_mapping_write_chains_against_latest_inflight_metadata_state(self):
        amu, _, tsu = _make_amu_fixture(with_phy=True)
        mvpn = 0
        mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
        amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        page = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = mvpn
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        page.valid_bitmap[0] = 1
        page.data[0] = 9000

        first_lpa = 1
        second_lpa = 2
        first_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=0)
        second_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=4, page=1)

        amu.cmt.cache = {first_lpa: cmt_entry(address=first_addr, dirty=True)}
        amu.cmt.lru_list = [first_lpa]
        amu.generate_mapping_write_transaction(amu.cmt.cache, mvpn)

        amu.cmt.cache = {second_lpa: cmt_entry(address=second_addr, dirty=True)}
        amu.cmt.lru_list = [second_lpa]
        amu.generate_mapping_write_transaction(amu.cmt.cache, mvpn)

        mapping_reads = [tr for tr in tsu.submitted if tr.type == TransactionType.MAPPING_READ]
        mapping_writes = [tr for tr in tsu.submitted if tr.type == TransactionType.MAPPING_WRITE]
        first_write, second_write = mapping_writes

        self.assertEqual(len(mapping_reads), 1)
        self.assertIn(first_write, second_write.rely_on_transactions)

        first_read = mapping_reads[0]
        first_read.response = tsu.phy._read_from_storage(first_read)
        first_write.get_response_from_transaction(first_read)
        second_write.get_response_from_transaction(first_write)
        tsu.phy._write_to_storage(second_write)

        merged = tsu.phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
        self.assertEqual(merged.valid_bitmap[0], 1)
        self.assertEqual(merged.data[0], 9000)
        self.assertEqual(merged.valid_bitmap[1], 1)
        self.assertEqual(merged.data[1], utils.translate_address_to_ppa(first_addr))
        self.assertEqual(merged.valid_bitmap[2], 1)
        self.assertEqual(merged.data[2], utils.translate_address_to_ppa(second_addr))

    def test_internal_mapping_read_invariant_fails_explicitly(self):
        phy = PHY()
        addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=0, page=0)
        page = phy._storage[0][0][0][0][0][0]
        page.function = PageType.MAPPING
        page.lpa = INVALID_LPA
        page.mvpn = 1
        page.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        page.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        page.valid_bitmap[0] = 1
        page.data[0] = 999
        page.valid_bitmap[2] = 1

        invalid_slot_read = Transaction(
            source_req=None,
            type=TransactionType.MAPPING_READ,
            mvpn=1,
            address=addr,
            bitmap=[0, 1] + [0] * (LPA_NO_PER_MAPPING_PAGE - 2),
        )
        invalid_ppa_read = Transaction(
            source_req=None,
            type=TransactionType.MAPPING_READ,
            mvpn=1,
            address=addr,
            bitmap=[0, 0, 1] + [0] * (LPA_NO_PER_MAPPING_PAGE - 3),
        )

        invalid_slot_page = phy._read_from_storage(invalid_slot_read)
        invalid_ppa_page = phy._read_from_storage(invalid_ppa_read)

        self.assertEqual(invalid_slot_page.function, PageType.MAPPING)
        self.assertEqual(invalid_slot_page.valid_bitmap, [0] * LPA_NO_PER_MAPPING_PAGE)
        self.assertEqual(invalid_slot_page.data, [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE)
        self.assertEqual(invalid_ppa_page.function, PageType.MAPPING)
        self.assertEqual(invalid_ppa_page.valid_bitmap, [0] * LPA_NO_PER_MAPPING_PAGE)
        self.assertEqual(invalid_ppa_page.data, [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE)

    def test_cmt_hit_overwrite_marks_old_page_invalid_immediately(self):
        amu, bm, tsu = _make_amu_fixture()
        bm.apply_runtime_config(
            RuntimeConfig(gc_low_watermark=3, stop_servicing_writes_threshold=1)
        )
        old_addr = FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=9, page=3)
        bm._mark_valid(old_addr)
        amu.cmt.cache[13] = cmt_entry(address=old_addr, dirty=True)
        amu.cmt.lru_list = [13]
        plane_addr = amu.get_plane_address_for_lpa(13)
        plane_bke = bm.get_plane_bke(plane_addr)
        plane_bke.free_block_pool = {plane_bke.write_frontier_block}

        req = Request(type=RequestType.WRITE, sq_id=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=13)
        req.transaction_list = [tr]

        amu.translate_and_submit(req)

        block_bke = bm.get_block_bke(old_addr)
        self.assertEqual(tsu.submitted, [tr])
        self.assertEqual(tr.invalidate_target, old_addr)
        self.assertNotIn(bm._plane_key(plane_addr), bm.waiting_writes)
        self.assertNotIn(old_addr.page, block_bke.valid_pages)
        self.assertIn(old_addr.page, block_bke.invalid_pages)

    def test_overlapping_same_lpa_writes_promote_barrier_owner_in_fifo_order(self):
        _, bm, _ = _make_amu_fixture()
        first = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=8,
            address=FlashAddress(
                channel=0,
                chip=0,
                die=0,
                plane=0,
                sub_plane=0,
                page=1,
            ),
        )
        second = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=8,
            address=FlashAddress(
                channel=0,
                chip=0,
                die=0,
                plane=0,
                sub_plane=0,
                page=2,
            ),
        )
        third = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=8,
            address=FlashAddress(
                channel=0,
                chip=0,
                die=0,
                plane=0,
                sub_plane=0,
                page=3,
            ),
        )

        bm._set_barrier(first)
        bm._set_barrier(second)
        bm._set_barrier(third)

        self.assertIs(bm.lpa_protected_book.get(8), first)
        self.assertEqual(bm.lpa_barrier_waiters.get(8), [second, third])

        bm._on_transaction_serviced(first)

        self.assertIs(bm.lpa_protected_book.get(8), second)
        self.assertEqual(bm.lpa_barrier_waiters.get(8), [third])

        bm._on_transaction_serviced(second)

        self.assertIs(bm.lpa_protected_book.get(8), third)
        self.assertNotIn(8, bm.lpa_barrier_waiters)

        bm._on_transaction_serviced(third)

        self.assertNotIn(8, bm.lpa_protected_book)
        self.assertNotIn(8, bm.lpa_barrier_waiters)

    def test_overlapping_same_lpa_write_completions_preserve_mapping_and_bookkeeping(self):
        amu, bm, tsu = _make_amu_fixture()
        lpa = 9
        transactions = []

        for _ in range(3):
            req = Request(type=RequestType.WRITE, sq_id=0)
            tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=lpa)
            req.transaction_list = [tr]
            amu.translate_and_submit(req)
            transactions.append(tr)

        first, second, third = transactions
        self.assertIs(bm.lpa_protected_book[lpa], first)
        self.assertEqual(bm.lpa_barrier_waiters[lpa], [second, third])
        self.assertEqual(tsu.submitted, transactions)

        for tr in transactions:
            bm._on_transaction_serviced(tr)

        plane_bke = bm.get_plane_bke(third.address)
        block_bke = bm.get_block_bke(third.address)
        self.assertEqual(amu.cmt.cache[lpa].address, third.address)
        self.assertEqual(block_bke.valid_pages & {tr.address.page for tr in transactions}, {third.address.page})
        self.assertEqual(block_bke.invalid_pages & {tr.address.page for tr in transactions}, {first.address.page, second.address.page})
        self.assertTrue(block_bke.valid_pages.isdisjoint(block_bke.invalid_pages))
        self.assertEqual(
            block_bke.free_page_count + block_bke.valid_page_count + block_bke.invalid_page_count,
            PAGE_PER_BLOCK,
        )
        self.assertEqual(plane_bke.valid_page_count, sum(bke.valid_page_count for bke in plane_bke.block_entries))
        self.assertEqual(plane_bke.invalid_page_count, sum(bke.invalid_page_count for bke in plane_bke.block_entries))
        self.assertEqual(plane_bke.free_page_count, sum(bke.free_page_count for bke in plane_bke.block_entries))
        self.assertNotIn(lpa, bm.lpa_protected_book)
        self.assertNotIn(lpa, bm.lpa_barrier_waiters)

    def test_report_exports_maintenance_summary(self):
        recorder = RequestLatencyRecorder()
        req = Request(
            type=RequestType.WRITE,
            size=64,
            trace_index=0,
            trace_time=0,
            report_req_id="req-write",
        )
        recorder.register_request(req, scheduled_time=0)
        tr = Transaction(source_req=req, type=TransactionType.USER_WRITE, lpa=0)
        gc_tr = Transaction(source_req=None, type=TransactionType.GC_WRITE, lpa=0)

        recorder.note_physical_write(tr)
        recorder.note_physical_write(gc_tr)
        recorder.note_gc_started(
            "gc",
            FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=-1, page=-1),
            victim_block=3,
            valid_page_count=2,
            invalid_page_count=5,
        )
        recorder.note_gc_erase_completed(
            FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=3, page=0),
            wl_level=1,
        )

        maintenance = recorder.export()["meta"]["maintenance"]

        self.assertEqual(maintenance["gc_count"], 1)
        self.assertEqual(maintenance["gc_relocated_pages"], 2)
        self.assertEqual(maintenance["gc_erased_blocks"], 1)
        self.assertEqual(maintenance["host_write_pages"], 1)
        self.assertEqual(maintenance["write_amplification"], 2.0)


if __name__ == "__main__":
    unittest.main()
