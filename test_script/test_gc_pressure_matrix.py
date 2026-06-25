"""Validation rules for the GC pressure-matrix summary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flash_sim.common import SECTOR_PER_PAGE
from flash_sim.gc_pressure_matrix import (
    PRESSURE_TRACES,
    SPECIAL_PRESSURE_TRACES,
    _summarize_report,
    _validate_result,
    _warnings_for_result,
)
from test_script.generate_gc_pressure_trace import plane_key_for_lpa


REPO_ROOT = Path(__file__).resolve().parents[1]
TRACE_DIR = REPO_ROOT / "test_case"
EXPECTED_TIME_STEPS = {
    "gc_pressure_trace": 2_000_000,
    "gc_pressure_trace_fast": 10,
    "gc_pressure_trace_slow": 2_000_000,
    "gc_pressure_trace_slow2": 20_000_000,
    "gc_pressure_trace_wide": 20_000_000,
    "gc_pressure_trace_5000000ns": 5_000_000,
    "gc_pressure_trace_10000000ns": 10_000_000,
    "gc_pressure_trace_15000000ns": 15_000_000,
    "gc_pressure_trace_20ms": 20_000_000,
}
def _load_trace(stem: str) -> list[dict]:
    with (TRACE_DIR / f"{stem}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_lpas(commands: list[dict]) -> list[int]:
    return [
        command["start_lha"] // SECTOR_PER_PAGE
        for command in commands
        if command["type"].lower() == "write"
    ]


def _result(**overrides):
    result = {
        "status": "ok",
        "error": 0,
        "error_messages": {},
        "incomplete": 0,
        "residual_waiting_writes": 0,
        "pending_cache_entries": 0,
        "report_path": "report/result.json",
        "csv_path": "report/result.csv",
        "write_requests": 10,
        "gc_count": 1,
        "static_wl_count": 0,
        "gc_relocated_pages": 2,
        "gc_erased_blocks": 1,
        "host_write_pages": 10,
        "physical_user_write_pages": 9,
        "physical_gc_write_pages": 2,
        "write_amplification": 1.1,
    }
    result.update(overrides)
    return result


def test_matrix_accepts_consistent_completed_maintenance_events():
    result = _result()

    assert _validate_result(result) == []
    assert _warnings_for_result(result) == []


def test_matrix_summary_extracts_physical_write_counters(tmp_path):
    report_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    report_path.write_text(
        json.dumps(
            {
                "meta": {
                    "final_time": 123,
                    "maintenance": {
                        "gc_count": 1,
                        "static_wl_count": 0,
                        "gc_relocated_pages": 2,
                        "gc_erased_blocks": 1,
                        "host_write_pages": 10,
                        "physical_user_write_pages": 9,
                        "physical_gc_write_pages": 2,
                        "write_amplification": 1.1,
                    },
                },
                "requests": [{"type": "WRITE", "status": "SUCCESS"}],
            }
        ),
        encoding="utf-8",
    )
    csv_path.write_text("header\n", encoding="utf-8")
    cache = SimpleNamespace(user_entries={}, static_entries={})
    engine = SimpleNamespace(
        device=SimpleNamespace(
            hil=SimpleNamespace(cache_manager=SimpleNamespace(cache=cache)),
            ftl=SimpleNamespace(
                block_manager=SimpleNamespace(waiting_writes={}),
            ),
        ),
    )

    result = _summarize_report(
        trace="gc-pressure",
        elapsed_s=0.5,
        report_path=report_path,
        csv_path=csv_path,
        engine=engine,
    )

    assert result["host_write_pages"] == 10
    assert result["physical_user_write_pages"] == 9
    assert result["physical_gc_write_pages"] == 2
    assert _validate_result(result) == []


def test_matrix_treats_request_errors_as_correctness_issues():
    result = _result(
        error=2,
        error_messages={"metadata lookup failed": 2},
    )

    issues = _validate_result(result)

    assert any("ERROR" in issue for issue in issues)
    assert _warnings_for_result(result) == []


def test_matrix_detects_gc_event_conservation_mismatches():
    result = _result(
        gc_count=2,
        gc_erased_blocks=1,
        gc_relocated_pages=3,
        physical_gc_write_pages=1,
        write_amplification=9.0,
    )

    issues = _validate_result(result)

    assert any("erase" in issue.lower() for issue in issues)
    assert any("relocat" in issue.lower() for issue in issues)
    assert any("amplification" in issue.lower() for issue in issues)


def test_matrix_keeps_subunit_write_amplification_as_workload_warning():
    result = _result(
        gc_count=0,
        gc_relocated_pages=0,
        gc_erased_blocks=0,
        host_write_pages=10,
        physical_user_write_pages=8,
        physical_gc_write_pages=0,
        write_amplification=0.8,
    )

    assert _validate_result(result) == []
    warnings = _warnings_for_result(result)
    assert len(warnings) == 1
    assert "coalescing" in warnings[0]


@pytest.mark.parametrize("trace", PRESSURE_TRACES)
def test_pressure_trace_has_stable_schema_and_timing_profile(trace):
    commands = _load_trace(trace)

    assert len(commands) == (529 if trace == "gc_pressure_trace_wide" else 518)
    assert all(command["type"].lower() in {"read", "write"} for command in commands)
    assert all(command["size"] > 0 for command in commands)
    assert all(command["time"] >= 0 for command in commands)
    assert [command["time"] for command in commands] == sorted(
        command["time"] for command in commands
    )

    steps = {
        commands[index]["time"] - commands[index - 1]["time"]
        for index in range(1, len(commands))
    }
    assert steps == {EXPECTED_TIME_STEPS[trace]}

    write_lpas = _write_lpas(commands)
    if trace == "gc_pressure_trace_wide":
        assert len(write_lpas) == 513
        assert len(set(write_lpas)) == 512
        assert {plane_key_for_lpa(lpa) for lpa in write_lpas} == {
            (0, 0, 0, 0),
            (0, 0, 0, 1),
        }
    else:
        assert len(write_lpas) == 492
        assert len(set(write_lpas)) == 424
        assert {plane_key_for_lpa(lpa) for lpa in write_lpas} == {(0, 0, 0, 0)}


def test_wide_pressure_trace_expands_the_working_set_instead_of_aliasing_20ms():
    wide = _load_trace("gc_pressure_trace_wide")
    reference = _load_trace("gc_pressure_trace_20ms")

    assert wide != reference
    assert len(set(_write_lpas(wide))) > len(set(_write_lpas(reference)))


def test_specialized_gc_pressure_regressions_are_present():
    missing = [
        trace
        for trace in SPECIAL_PRESSURE_TRACES
        if not (TRACE_DIR / f"{trace}.json").is_file()
    ]

    assert missing == []


def test_low_invalid_trace_reaches_gc_pressure_with_one_overwrite():
    writes = _write_lpas(_load_trace("gc_pressure_low_invalid"))

    assert len(writes) == 481
    assert len(set(writes)) == 480
    assert writes[-1] == writes[0]


def test_concurrent_overwrite_trace_contains_dense_duplicate_writes():
    commands = _load_trace("gc_pressure_concurrent_overwrite")
    writes = _write_lpas(commands)

    assert {command["time"] for command in commands} == {0}
    assert len(writes) > len(set(writes))


def test_post_flush_trace_has_a_pause_before_sustained_writes():
    commands = _load_trace("gc_pressure_post_flush_sustained")
    gaps = [
        commands[index]["time"] - commands[index - 1]["time"]
        for index in range(1, len(commands))
    ]
    pause_index = gaps.index(max(gaps)) + 1

    assert max(gaps) >= 50_000_000
    assert sum(command["type"] == "write" for command in commands[pause_index:]) >= 200


def test_gc_reoverwrite_trace_updates_a_live_victim_lpa_during_gc_window():
    commands = _load_trace("gc_pressure_gc_reoverwrite")
    writes = [command for command in commands if command["type"] == "write"]
    victim_lpa = writes[1]["start_lha"] // SECTOR_PER_PAGE
    rewrites = [
        command
        for command in writes
        if command["start_lha"] // SECTOR_PER_PAGE == victim_lpa
    ]

    assert len(writes) == 482
    assert len(rewrites) == 2
    assert rewrites[1]["time"] - writes[-2]["time"] == 1_000_000
