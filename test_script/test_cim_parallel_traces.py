"""End-to-end parallelism checks for the simple CIM trace fixtures."""

import io
import random
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from flash_sim.common import (
    BLOCK_PER_PLANE,
    PLANE_PER_DIE,
    SL_PER_BLOCK,
    SSL_PER_SL,
    STATIC_BASE_LHA,
    FlashAddress,
    Request,
    RequestFailure,
    RequestType,
    Transaction,
    TransactionType,
)
from flash_sim.config import FlashConfig
from flash_sim.engine import Engine
from flash_sim.PHY import onfi_data_in_duration, onfi_data_out_duration


TRACE_DIR = Path(__file__).resolve().parents[1] / "test_case" / "cim_parallel"


def _run_trace(trace_name, config=None):
    output = io.StringIO()
    engine = None
    try:
        with redirect_stdout(output), redirect_stderr(output):
            random.seed(0)
            engine = Engine(config=config)
            engine.Start_simulation(str(TRACE_DIR / trace_name))
        return engine.request_latency_recorder.export(), output.getvalue()
    finally:
        if engine is not None:
            for report_path in (
                engine.last_request_latency_report_path,
                engine.last_request_latency_csv_path,
            ):
                if report_path is not None:
                    report_path.unlink(missing_ok=True)


def _array_intervals(report):
    requests = report["requests"][-2:] if len(report["requests"]) > 2 else report["requests"]
    return sorted(
        (
            interval
            for request in requests
            for interval in request["intervals"]["phy_array_exec"]
        ),
        key=lambda interval: (interval["start"], interval["end"]),
    )


def _overlap(first, second):
    return max(first["start"], second["start"]) < min(first["end"], second["end"])


def _phase_wave_groups(request, phase):
    counts = Counter(
        (interval["start"], interval["end"])
        for interval in request["intervals"][phase]
    )
    return sorted(
        (start, end, transaction_count)
        for (start, end), transaction_count in counts.items()
    )


def _peak_array_concurrency(report):
    events = []
    for request in report["requests"]:
        for interval in request["intervals"]["phy_array_exec"]:
            events.append((interval["start"], 1))
            events.append((interval["end"], -1))
    active = peak = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        peak = max(peak, active)
    return peak


def test_parallel_trace_addresses_match_default_runtime_geometry():
    assert STATIC_BASE_LHA == 12_582_912
    assert SL_PER_BLOCK == 2
    assert SSL_PER_SL == 4
    assert BLOCK_PER_PLANE == 64
    assert BLOCK_PER_PLANE * SL_PER_BLOCK * SSL_PER_SL == 512
    assert PLANE_PER_DIE * BLOCK_PER_PLANE * SL_PER_BLOCK * SSL_PER_SL == 2_048


@pytest.mark.parametrize(
    ("trace_name", "expected_parallel"),
    [
        ("compute_same_sl_serial.json", False),
        ("compute_different_sl_parallel.json", True),
        ("compute_cross_plane_parallel.json", True),
        ("compute_cross_die_independent.json", True),
        ("compute_same_die_requests_serial.json", False),
        ("search_same_plane_serial.json", False),
        ("search_cross_plane_parallel.json", True),
        ("search_same_die_requests_serial.json", False),
        ("search_cross_die_independent.json", True),
    ],
)
def test_cim_trace_array_parallelism(trace_name, expected_parallel):
    report, output = _run_trace(trace_name)

    assert "Traceback" not in output
    assert all(request["status"] == "SUCCESS" for request in report["requests"])
    intervals = _array_intervals(report)
    assert len(intervals) == 2
    assert _overlap(intervals[0], intervals[1]) is expected_parallel

    if not expected_parallel:
        assert intervals[0]["end"] <= intervals[1]["start"]


def test_compute_full_die_uses_four_512_transaction_waves():
    report, output = _run_trace("compute_full_die.json")

    assert "Traceback" not in output
    request = report["requests"][0]
    assert request["status"] == "SUCCESS"
    assert request["size"] == 2_048

    expected_phase_durations = {
        "phy_cmd_addr": 2_809,
        "phy_data_in": 192,
        "phy_array_exec": 500_000,
        "phy_data_out": 394_986,
    }
    for phase, expected_duration in expected_phase_durations.items():
        groups = _phase_wave_groups(request, phase)
        assert len(groups) == 4
        assert [transaction_count for _, _, transaction_count in groups] == [512] * 4
        assert [end - start for start, end, _ in groups] == [expected_duration] * 4


def test_search_full_die_uses_512_four_plane_waves():
    report, output = _run_trace("search_full_die.json")

    assert "Traceback" not in output
    request = report["requests"][0]
    assert request["status"] == "SUCCESS"
    assert request["size"] == 2_048

    expected_phase_durations = {
        "phy_cmd_addr": 2_809,
        "phy_data_in": 6,
        "phy_array_exec": 200_000,
        "phy_data_out": 50_922,
    }
    for phase, expected_duration in expected_phase_durations.items():
        groups = _phase_wave_groups(request, phase)
        assert len(groups) == 512
        assert [transaction_count for _, _, transaction_count in groups] == [4] * 512
        assert [end - start for start, end, _ in groups] == [expected_duration] * 512


@pytest.mark.parametrize(
    ("compute_max_parallel_sl", "expected_waves"),
    [(1, 512), (32, 16), (64, 8)],
)
def test_compute_full_die_obeys_configured_sl_limit(
    compute_max_parallel_sl, expected_waves
):
    config = FlashConfig.from_dict(
        {"geometry": {"compute_max_parallel_sl": compute_max_parallel_sl}}
    )

    report, output = _run_trace("compute_full_die.json", config=config)

    assert "Traceback" not in output
    request = report["requests"][0]
    assert request["status"] == "SUCCESS"
    groups = _phase_wave_groups(request, "phy_array_exec")
    assert len(groups) == expected_waves
    assert [count for _, _, count in groups] == [4 * compute_max_parallel_sl] * expected_waves


@pytest.mark.parametrize(
    ("trace_name", "expected_peak"),
    [("compute_full_chip.json", 2_048), ("search_full_chip.json", 16)],
)
def test_full_chip_reaches_four_die_peak_array_concurrency(trace_name, expected_peak):
    report, output = _run_trace(trace_name)

    assert "Traceback" not in output
    request = report["requests"][0]
    assert request["status"] == "SUCCESS"
    assert request["size"] == 8_192
    assert len(request["intervals"]["phy_array_exec"]) == 8_192
    assert _peak_array_concurrency(report) == expected_peak


def test_engine_propagates_cim_geometry_and_onfi_timing_to_runtime_layers():
    config = FlashConfig.from_dict(
        {
            "onfi": {"channel_width_bytes": 16},
            "geometry": {
                "wl_per_string": 64,
                "bl_per_plane": 131_072,
                "search_input_bits_per_wl": 2,
                "search_match_bits_per_bl": 2,
                "compute_input_bits_per_sl": 4,
                "compute_accumulator_bits": 4,
                "compute_max_parallel_sl": 32,
            },
        }
    )
    engine = Engine(config=config)
    phy = engine.device.phy

    assert engine.device.hil.wl_per_string == 64
    assert engine.device.ftl.tsu.compute_max_parallel_sl == 32
    assert phy.onfi_timing.channel_width_bytes == 16

    search_req = Request(type=RequestType.SEARCH)
    compute_req = Request(type=RequestType.COMPUTE, selected_wl=63)
    search_transactions = []
    compute_transactions = []
    for plane in (0, 1):
        address = FlashAddress(
            channel=0, chip=0, die=0, plane=plane, sub_plane=0, page=-1
        )
        search_transactions.append(
            Transaction(
                source_req=search_req,
                type=TransactionType.USER_SEARCH,
                address=address,
            )
        )
        compute_transactions.append(
            Transaction(
                source_req=compute_req,
                type=TransactionType.USER_COMPUTE,
                address=address,
            )
        )

    assert phy._data_in_payload_bytes("search", search_transactions) == 16
    assert phy._data_out_payload_bytes("search", search_transactions) == 65_536
    assert phy._data_in_payload_bytes("compute", compute_transactions) == 1
    assert phy._data_out_payload_bytes("compute", compute_transactions) == 131_072
    assert phy._data_in_transfer_duration(search_transactions, "search") == onfi_data_in_duration(
        16, config.onfi
    )
    assert phy._data_out_transfer_duration(compute_transactions, "compute") == onfi_data_out_duration(
        131_072, 2, config.onfi
    )

    engine.device.hil._validate_request_domain(
        Request(
            type=RequestType.COMPUTE,
            lha_start=STATIC_BASE_LHA,
            size=1,
            selected_wl=63,
        )
    )
    with pytest.raises(RequestFailure, match=r"\[0, 64\)"):
        engine.device.hil._validate_request_domain(
            Request(
                type=RequestType.COMPUTE,
                lha_start=STATIC_BASE_LHA,
                size=1,
                selected_wl=64,
            )
        )
