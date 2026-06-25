"""End-to-end Static WL validation through Engine, TSU, and PHY."""

import io
from contextlib import redirect_stderr, redirect_stdout

from flash_sim.PHY import PageType
from flash_sim.common import (
    FlashAddress,
    PAGE_PER_BLOCK,
    SECTOR_PER_PAGE,
    cmt_entry,
)
from flash_sim.engine import Engine


def test_static_wl_relocates_cold_data_through_real_engine_path():
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        engine = Engine()
        ftl = engine.device.ftl
        bm = ftl.block_manager
        amu = ftl.address_mapping_unit
        unit = ftl.gc_wl_unit
        phy = engine.device.phy
        plane_addr = FlashAddress(
            channel=0,
            chip=0,
            die=0,
            plane=0,
            sub_plane=-1,
            page=-1,
        )
        plane_bke = bm.get_plane_bke(plane_addr)
        source_block = 2
        destination_block = 6
        lpa = 101
        source_addr = FlashAddress(
            channel=0,
            chip=0,
            die=0,
            plane=0,
            sub_plane=source_block,
            page=0,
        )

        for block in plane_bke.block_entries:
            block.wl_level = 1
        source_bke = plane_bke.block_entries[source_block]
        source_bke.wl_level = 0
        source_bke.write_frontier = 1
        source_bke.free_page_count = PAGE_PER_BLOCK - 1
        source_bke.valid_pages = {source_addr.page}
        source_bke.valid_page_count = 1
        plane_bke.block_entries[destination_block].wl_level = 2
        plane_bke.free_block_pool.discard(source_block)
        plane_bke.free_page_count -= 1
        plane_bke.valid_page_count += 1

        page = phy._storage[0][0][0][0][source_block][source_addr.page]
        page.function = PageType.USER
        page.lpa = lpa
        page.valid_bitmap = [1] * SECTOR_PER_PAGE
        page.data = [77] * SECTOR_PER_PAGE
        amu.cmt.cache[lpa] = cmt_entry(address=source_addr, dirty=False)
        amu.cmt.lru_list = [lpa]

        unit.on_erase_complete(plane_addr, reason="gc")
        assert not engine.event_queue.empty()
        engine.Run()

    maintenance = engine.request_latency_recorder.export()["meta"]["maintenance"]
    new_addr = amu.cmt.cache[lpa].address
    new_page = phy._storage[
        new_addr.channel
    ][new_addr.chip][new_addr.die][new_addr.plane][new_addr.sub_plane][new_addr.page]

    assert "Traceback" not in output.getvalue()
    assert new_addr.sub_plane == destination_block
    assert new_addr != source_addr
    assert new_page.function == PageType.USER
    assert new_page.lpa == lpa
    assert new_page.data == [77] * SECTOR_PER_PAGE
    assert source_bke.wl_level == 1
    assert source_block in plane_bke.free_block_pool
    assert plane_bke.gc_wl_barrier_blocks == set()
    assert maintenance["gc_count"] == 0
    assert maintenance["static_wl_count"] == 1
    assert maintenance["gc_relocated_pages"] == 1
    assert maintenance["physical_gc_write_pages"] == 1
    assert maintenance["gc_erased_blocks"] == 1
