import json
from pathlib import Path

import pytest

from flash_sim.common import STATIC_BASE_LHA
from test_script import request_resource_contention_experiments as exp


def _fake_report_for_trace(trace_path, *, read_delta=0):
    commands = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    requests = []
    for index, command in enumerate(commands):
        request_type = command["type"].upper()
        base_latency = int(command["size"]) * 10
        if request_type == "COMPUTE":
            base_latency += 100
        elif request_type == "SEARCH":
            base_latency += 50
        elif request_type == "READ":
            base_latency += 20 + read_delta
        completion_time = int(command["time"]) + base_latency
        requests.append(
            {
                "trace_index": index,
                "type": request_type,
                "scheduled_time": command["time"],
                "trace_time": command["time"],
                "lha_start": command["start_lha"],
                "size": command["size"],
                "host_completion_time": completion_time,
                "total_latency": base_latency,
            }
        )
    return {"meta": {"request_count": len(requests)}, "requests": requests}


def test_size_validation_and_static_single_request_trace_planning():
    assert exp.validate_scan_sizes([1, "2", 4]) == (1, 2, 4)
    with pytest.raises(ValueError, match="positive"):
        exp.validate_scan_sizes([0])

    command = exp.build_static_request_command("compute", 4)
    assert command == {
        "type": "compute",
        "time": 0,
        "start_lha": STATIC_BASE_LHA,
        "size": 4,
    }
    assert command["start_lha"] + command["size"] <= exp.static_region_end_exclusive()

    plans = exp.plan_size_scan_traces([1, 2, 4])
    assert len(plans) == 6
    assert {(plan["request_type"], plan["size"]) for plan in plans} == {
        ("compute", 1),
        ("compute", 2),
        ("compute", 4),
        ("search", 1),
        ("search", 2),
        ("search", 4),
    }
    assert all(len(plan["commands"]) == 1 for plan in plans)


def test_size_scan_writes_traces_aggregate_outputs_and_svg_charts(tmp_path):
    calls = []

    def fake_simulate(trace_path, **_kwargs):
        calls.append(Path(trace_path))
        return exp.SimulationResult(
            trace_path=Path(trace_path),
            report_path=Path(trace_path).with_suffix(".report.json"),
            report=_fake_report_for_trace(trace_path),
        )

    result = exp.run_size_scan([1, 2], output_root=tmp_path, simulate=fake_simulate)

    assert result["simulation_count"] == 4
    assert len(calls) == 4
    assert len(result["trace_paths"]) == 4
    assert Path(result["results_json"]).exists()
    assert Path(result["results_csv"]).exists()
    assert Path(result["chart_paths"]["compute"]).exists()
    assert Path(result["chart_paths"]["search"]).exists()

    payload = json.loads(Path(result["results_json"]).read_text(encoding="utf-8"))
    assert payload["normalization"] == "per_request_type_max_total_latency"
    assert {row["request_type"] for row in payload["results"]} == {"compute", "search"}
    compute_rows = [row for row in payload["results"] if row["request_type"] == "compute"]
    assert [row["normalized_latency"] for row in compute_rows] == pytest.approx([110 / 120, 1.0])

    svg_text = Path(result["chart_paths"]["compute"]).read_text(encoding="utf-8")
    assert "COMPUTE normalized latency by size" in svg_text
    assert ">1<" in svg_text
    assert ">2<" in svg_text


def test_normalization_preserves_raw_values_and_handles_zero_max():
    rows = [
        {"request_type": "compute", "size": 1, "total_latency": 10},
        {"request_type": "compute", "size": 2, "total_latency": 20},
        {"request_type": "search", "size": 1, "total_latency": 5},
        {"request_type": "search", "size": 2, "total_latency": 10},
    ]

    normalized = exp.normalize_size_scan_results(rows)

    assert [row["total_latency"] for row in normalized] == [10, 20, 5, 10]
    assert [row["normalized_latency"] for row in normalized] == pytest.approx([0.5, 1.0, 0.5, 1.0])

    zero_normalized = exp.normalize_size_scan_results(
        [{"request_type": "compute", "size": 1, "total_latency": 0}]
    )
    assert zero_normalized[0]["normalized_latency"] == 0.0


def test_paired_read_impact_traces_have_identical_read_portions_and_compute_prefix():
    reads = [
        {"type": "read", "time": 1, "start_lha": 64, "size": 1},
        {"type": "read", "time": 101, "start_lha": 128, "size": 1},
    ]

    baseline, contended = exp.build_paired_read_impact_traces(read_commands=reads, compute_size=4)

    assert baseline == reads
    assert [command["type"] for command in contended[:2]] == ["compute", "compute"]
    assert contended[2:] == baseline
    assert exp.read_commands_only(contended) == baseline


def test_read_impact_comparison_outputs_matched_read_deltas(tmp_path):
    reads = [
        {"type": "read", "time": 1, "start_lha": 64, "size": 1},
        {"type": "read", "time": 101, "start_lha": 128, "size": 1},
    ]

    def fake_simulate(trace_path, **_kwargs):
        read_delta = 5 if "compute_contention" in str(trace_path) else 0
        return exp.SimulationResult(
            trace_path=Path(trace_path),
            report_path=Path(trace_path).with_suffix(".report.json"),
            report=_fake_report_for_trace(trace_path, read_delta=read_delta),
        )

    result = exp.run_read_impact_comparison(
        output_root=tmp_path,
        read_commands=reads,
        compute_size=4,
        simulate=fake_simulate,
    )

    assert Path(result["results_json"]).exists()
    assert Path(result["results_csv"]).exists()
    assert len(result["comparison"]) == 2
    assert all(row["completion_time_delta"] == 5 for row in result["comparison"])


def test_read_identity_mismatch_fails_explicitly():
    baseline_report = {
        "requests": [
            {
                "type": "READ",
                "scheduled_time": 1,
                "lha_start": 64,
                "size": 1,
                "host_completion_time": 20,
            }
        ]
    }
    contended_report = {
        "requests": [
            {
                "type": "READ",
                "scheduled_time": 2,
                "lha_start": 64,
                "size": 1,
                "host_completion_time": 25,
            }
        ]
    }

    with pytest.raises(ValueError, match="read identity mismatch"):
        exp.compare_read_completion_times(baseline_report, contended_report)


def test_run_engine_and_load_report_reads_json_latency_fields(tmp_path):
    trace_path = tmp_path / "single_compute.json"
    exp.write_trace([exp.build_static_request_command("compute", 3)], trace_path)

    class FakeEngine:
        def __init__(self):
            self.last_request_latency_report_path = None

        def Start_simulation(self, trace_path, pre_trace=None):
            report_path = Path(trace_path).with_name("single_compute_request_latency.json")
            report_path.write_text(json.dumps(_fake_report_for_trace(trace_path)), encoding="utf-8")
            self.last_request_latency_report_path = report_path

    simulation = exp.run_engine_and_load_report(trace_path, engine_factory=FakeEngine)
    row = exp.aggregate_size_scan_result("compute", 3, simulation)

    assert row["host_completion_time"] == 130
    assert row["total_latency"] == 130
