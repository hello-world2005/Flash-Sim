import json

import pytest

from flash_sim import common
from flash_sim.config import FlashConfig
from flash_sim.engine import Engine, _generate_precondition_from_trace


def _write_trace(tmp_path, commands):
    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps(commands), encoding="utf-8")
    return trace_path


def _make_cwdp_engine():
    config = FlashConfig()
    config.runtime.plane_allocation = "CWDP"
    config.runtime.cache_bypass = True
    return Engine(config=config)


@pytest.fixture(autouse=True)
def _quiet_engine():
    old_value = common.QUIET.value
    common.QUIET.value = True
    try:
        yield
    finally:
        common.QUIET.value = old_value


def test_capacity_fill_adds_deterministic_filler_beyond_trace_lpas(tmp_path):
    trace_path = _write_trace(
        tmp_path,
        [
            {"type": "read", "time": 0, "start_lha": 0, "size": common.SECTOR_PER_PAGE},
            {
                "type": "write",
                "time": 1,
                "start_lha": common.SECTOR_PER_PAGE,
                "size": common.SECTOR_PER_PAGE,
            },
        ],
    )
    engine = _make_cwdp_engine()

    capacity_plan = _generate_precondition_from_trace(
        str(trace_path),
        0.01,
        engine.device.ftl.block_manager,
        engine.device.ftl.address_mapping_unit,
        mode="capacity-fill",
        seed=7,
    )
    trace_cover_plan = _generate_precondition_from_trace(
        str(trace_path),
        0.01,
        engine.device.ftl.block_manager,
        engine.device.ftl.address_mapping_unit,
        mode="trace-cover",
        seed=7,
    )

    capacity_stats = capacity_plan["stats"]
    trace_cover_stats = trace_cover_plan["stats"]
    assert capacity_stats["filler_pages"] > 0
    assert capacity_stats["planned_pages"] > trace_cover_stats["planned_pages"]
    assert capacity_stats["planned_fill_ratio"] == pytest.approx(0.01, abs=0.002)
    assert trace_cover_stats["filler_pages"] == 0


def test_capacity_fill_stats_survive_ftl_preconditioning(tmp_path):
    trace_path = _write_trace(
        tmp_path,
        [{"type": "read", "time": 0, "start_lha": 0, "size": common.SECTOR_PER_PAGE}],
    )
    engine = _make_cwdp_engine()
    plan = _generate_precondition_from_trace(
        str(trace_path),
        0.01,
        engine.device.ftl.block_manager,
        engine.device.ftl.address_mapping_unit,
        mode="capacity-fill",
        seed=11,
    )

    block_manager = engine.device.ftl.block_manager
    block_manager.preconditioning(
        data_path=plan,
        phy=engine.device.ftl.tsu.phy,
        amu=engine.device.ftl.address_mapping_unit,
    )

    stats = block_manager.last_precondition_stats
    assert stats["mode"] == "capacity-fill"
    assert stats["actual_pages"] == plan["stats"]["planned_pages"]
    assert stats["dropped_pages"] == 0
    assert stats["actual_fill_ratio"] == pytest.approx(
        plan["stats"]["planned_fill_ratio"],
        abs=1e-9,
    )
