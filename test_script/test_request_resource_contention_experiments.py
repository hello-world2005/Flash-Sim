import json
from pathlib import Path

import pytest

from flash_sim.common import STATIC_BASE_LHA
from test_script import request_resource_contention_experiments as exp


def _precondition_record(lpa, valid_bitmap=None):
    if valid_bitmap is None:
        valid_bitmap = [1] * exp.SECTOR_PER_PAGE
    return {"lpa": int(lpa), "valid_bitmap": list(valid_bitmap)}


def _write_pre_data(tmp_path, records):
    path = tmp_path / "precondition_data.json"
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return path


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
        mapping_counts = {
            "cmt_hit": 1 if request_type == "READ" else 0,
            "gmt_hit": 0,
            "mapping_read": 0,
            "uncached_write": 0,
        }
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
                "mapping_resolution_counts": mapping_counts,
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
        "selected_wl": 0,
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
    assert "CIM normalized latency" in svg_text
    assert "COMPUTE normalized latency by size" not in svg_text
    assert "Normalized latency" in svg_text
    assert "Size" in svg_text
    assert ">1<" in svg_text
    assert ">2<" in svg_text
    assert ">0.92<" in svg_text
    assert ">1.00<" in svg_text
    assert ">0.917<" not in svg_text
    assert 'x="56" y="40" width="400" height="216" fill="none" stroke="#333"' in svg_text

    search_svg_text = Path(result["chart_paths"]["search"]).read_text(encoding="utf-8")
    assert "CAM normalized latency" in search_svg_text
    assert "SEARCH normalized latency by size" not in search_svg_text
    assert ">0.86<" in search_svg_text
    assert ">0.857<" not in search_svg_text


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


def _page_read(lpa, time):
    return {
        "type": "read",
        "time": int(time),
        "start_lha": int(lpa) * exp.SECTOR_PER_PAGE,
        "size": exp.SECTOR_PER_PAGE,
    }


def _first_unpreconditionable_lpa():
    for lpa in range(exp.read_impact_random_access_data_pages()):
        if not exp.read_impact_lpa_is_preconditionable(lpa):
            return lpa
    raise AssertionError("expected at least one runtime-skipped LPA for static chip coverage")


def test_read_impact_trace_plans_have_identical_page_read_portions():
    reads = [_page_read(lpa, 1 + index * 100) for index, lpa in enumerate(range(1, 11))]

    plans = exp.build_read_impact_trace_plans(read_commands=reads)
    baseline_reads = plans[0].commands

    assert plans[0].condition_id == "baseline"
    assert len(plans) == 9
    assert all(exp.read_commands_only(plan.commands) == baseline_reads for plan in plans)
    assert all(command["start_lha"] % exp.SECTOR_PER_PAGE == 0 for command in baseline_reads)
    assert all(command["size"] == exp.SECTOR_PER_PAGE for command in baseline_reads)


def test_read_impact_trace_plans_scan_compute_ratio_and_size():
    reads = [_page_read(lpa, 1 + index * 100) for index, lpa in enumerate(range(1, 11))]

    plans = exp.build_read_impact_trace_plans(read_commands=reads)
    ratio_plans = [plan for plan in plans if plan.group == exp.READ_IMPACT_RATIO_SCAN_GROUP]
    size_plans = [plan for plan in plans if plan.group == exp.READ_IMPACT_SIZE_SCAN_GROUP]

    assert [plan.configured_ratio for plan in ratio_plans] == [0.1, 0.2, 0.4, 0.8]
    assert [plan.num_compute_req for plan in ratio_plans] == [1, 2, 4, 8]
    assert all(plan.compute_size == 128 for plan in ratio_plans)
    for plan in ratio_plans:
        compute_commands = [command for command in plan.commands if command["type"] == "compute"]
        assert len(compute_commands) == plan.num_compute_req
        assert all(command["size"] == 128 for command in compute_commands)

    assert [plan.compute_size for plan in size_plans] == [8, 32, 128, 512]
    assert all(plan.configured_ratio == 0.2 for plan in size_plans)
    assert all(plan.num_compute_req == 2 for plan in size_plans)


def test_read_lpa_sector_ranges_handles_cross_lpa_reads():
    reads = [
        {
            "type": "read",
            "time": 1,
            "start_lha": exp.SECTOR_PER_PAGE - 1,
            "size": 3,
        }
    ]

    ranges = exp.read_lpa_sector_ranges(reads)

    assert ranges == {
        0: [(exp.SECTOR_PER_PAGE - 1, 1)],
        1: [(0, 2)],
    }


def test_read_impact_precondition_contains_only_touched_lpas(tmp_path):
    pre_data_path = _write_pre_data(
        tmp_path,
        [
            _precondition_record(1),
            _precondition_record(2),
            _precondition_record(3),
        ],
    )
    reads = [
        _page_read(1, 1),
        _page_read(3, 2),
    ]

    records = exp.select_read_impact_precondition_records(reads, pre_data_path=pre_data_path)
    precondition_path = exp.write_read_impact_precondition(tmp_path, records)

    assert [record["lpa"] for record in records] == [1, 3]
    assert precondition_path.exists()
    assert [record["lpa"] for record in json.loads(precondition_path.read_text(encoding="utf-8"))] == [1, 3]


def test_read_impact_precondition_validation_rejects_missing_or_invalid_sectors(tmp_path):
    pre_data_path = _write_pre_data(tmp_path, [_precondition_record(1)])
    missing_reads = [_page_read(2, 1)]

    with pytest.raises(ValueError, match="missing preconditioned LPA"):
        exp.select_read_impact_precondition_records(missing_reads, pre_data_path=pre_data_path)

    invalid_bitmap = [1] * exp.SECTOR_PER_PAGE
    invalid_bitmap[2] = 0
    invalid_pre_data_path = _write_pre_data(tmp_path, [_precondition_record(1, invalid_bitmap)])
    invalid_reads = [_page_read(1, 1)]

    with pytest.raises(ValueError, match="invalid sector"):
        exp.select_read_impact_precondition_records(invalid_reads, pre_data_path=invalid_pre_data_path)

    unaligned_reads = [
        {
            "type": "read",
            "time": 1,
            "start_lha": exp.SECTOR_PER_PAGE + 1,
            "size": exp.SECTOR_PER_PAGE,
        }
    ]
    with pytest.raises(ValueError, match="page-aligned page read"):
        exp.select_read_impact_precondition_records(unaligned_reads, pre_data_path=pre_data_path)

    short_reads = [{"type": "read", "time": 1, "start_lha": exp.SECTOR_PER_PAGE, "size": 1}]
    with pytest.raises(ValueError, match="page-aligned page read"):
        exp.select_read_impact_precondition_records(short_reads, pre_data_path=pre_data_path)


def test_read_impact_generation_skips_lpas_runtime_preconditioning_will_not_warm(tmp_path):
    skipped_lpa = _first_unpreconditionable_lpa()
    valid_lpas = [lpa for lpa in range(1, 16) if exp.read_impact_lpa_is_preconditionable(lpa)][:2]
    pre_data_path = _write_pre_data(
        tmp_path,
        [_precondition_record(skipped_lpa), *[_precondition_record(lpa) for lpa in valid_lpas]],
    )

    reads = exp.build_default_read_commands(pre_data_path=pre_data_path, read_count=2)

    assert [read["start_lha"] // exp.SECTOR_PER_PAGE for read in reads] == valid_lpas
    with pytest.raises(ValueError, match="not preconditionable"):
        exp.select_read_impact_precondition_records(
            [_page_read(skipped_lpa, 1)],
            pre_data_path=pre_data_path,
        )


def test_read_impact_precondition_validation_rejects_cmt_capacity_overflow():
    reads = [
        _page_read(1, 1),
        _page_read(2, 2),
    ]
    records = [_precondition_record(1), _precondition_record(2)]

    with pytest.raises(ValueError, match="exceed CMT warm capacity"):
        exp.validate_read_commands_for_cmt_hit(reads, records, cmt_capacity=1)


def test_read_impact_comparison_outputs_normalized_condition_rows(tmp_path):
    pre_data_path = _write_pre_data(tmp_path, [_precondition_record(1), _precondition_record(2)])
    reads = [
        _page_read(1, 1),
        _page_read(2, 101),
    ]
    pre_trace_paths = []

    def fake_simulate(trace_path, **kwargs):
        pre_trace_paths.append(Path(kwargs["pre_trace"]))
        read_delta = 0 if "baseline" in Path(trace_path).name else 5
        return exp.SimulationResult(
            trace_path=Path(trace_path),
            report_path=Path(trace_path).with_suffix(".report.json"),
            report=_fake_report_for_trace(trace_path, read_delta=read_delta),
        )

    result = exp.run_read_impact_comparison(
        output_root=tmp_path,
        pre_data_path=pre_data_path,
        read_commands=reads,
        simulate=fake_simulate,
    )

    assert Path(result["results_json"]).exists()
    assert Path(result["results_csv"]).exists()
    assert Path(result["chart_path"]).exists()
    assert Path(result["cmt_hit_precondition_path"]).exists()
    assert pre_trace_paths == [Path(result["cmt_hit_precondition_path"])] * 9
    assert result["simulation_count"] == 9
    assert len(result["results"]) == 9

    baseline_row = next(row for row in result["results"] if row["condition_id"] == "baseline")
    assert baseline_row["num_compute_req"] == 0
    assert baseline_row["normalized_latency"] == 1.0
    assert baseline_row["average_read_latency"] == exp.SECTOR_PER_PAGE * 10 + 20

    ratio_rows = [row for row in result["results"] if row["group"] == exp.READ_IMPACT_RATIO_SCAN_GROUP]
    size_rows = [row for row in result["results"] if row["group"] == exp.READ_IMPACT_SIZE_SCAN_GROUP]
    assert [row["parameter_value"] for row in ratio_rows] == [0.1, 0.2, 0.4, 0.8]
    assert [row["compute_size"] for row in size_rows] == [8, 32, 128, 512]
    assert all(row["normalized_latency"] > 1.0 for row in ratio_rows + size_rows)

    payload = json.loads(Path(result["results_json"]).read_text(encoding="utf-8"))
    assert payload["normalization"] == "baseline_average_read_latency"
    assert len(payload["results"]) == 9


def test_read_impact_average_latency_and_normalization_ignore_compute_rows():
    reads = [_page_read(1, 1), _page_read(2, 101)]
    plans = exp.build_read_impact_trace_plans(
        read_commands=reads,
        ratio_scan_values=[0.5],
        size_scan_values=[8],
    )

    def simulation_for(plan, read_latencies):
        requests = []
        read_index = 0
        for index, command in enumerate(plan.commands):
            request_type = command["type"].upper()
            total_latency = 999 if request_type == "COMPUTE" else read_latencies[read_index]
            if request_type == "READ":
                read_index += 1
            requests.append(
                {
                    "trace_index": index,
                    "type": request_type,
                    "scheduled_time": command["time"],
                    "lha_start": command["start_lha"],
                    "size": command["size"],
                    "host_completion_time": command["time"] + total_latency,
                    "total_latency": total_latency,
                    "mapping_resolution_counts": {
                        "cmt_hit": 1 if request_type == "READ" else 0,
                        "gmt_hit": 0,
                        "mapping_read": 0,
                        "uncached_write": 0,
                    },
                }
            )
        return exp.SimulationResult(
            trace_path=Path(f"{plan.condition_id}.json"),
            report_path=Path(f"{plan.condition_id}.report.json"),
            report={"requests": requests},
        )

    simulations = {
        plans[0].condition_id: simulation_for(plans[0], [10, 30]),
        plans[1].condition_id: simulation_for(plans[1], [20, 40]),
        plans[2].condition_id: simulation_for(plans[2], [40, 80]),
    }

    rows = exp.build_read_impact_result_rows(plans, simulations)

    assert [row["average_read_latency"] for row in rows] == [20, 30, 60]
    assert [row["normalized_latency"] for row in rows] == [1.0, 1.5, 3.0]
    assert rows[1]["num_compute_req"] == 1


def test_read_impact_grouped_chart_uses_labels_colors_and_two_decimal_values(tmp_path):
    rows = [
        {
            "condition_id": "baseline",
            "group": exp.READ_IMPACT_BASELINE_GROUP,
            "group_label": "Control",
            "parameter_label": "control",
            "normalized_latency": 1.0,
        },
        {
            "condition_id": "ratio_0_1",
            "group": exp.READ_IMPACT_RATIO_SCAN_GROUP,
            "group_label": "Insertion ratio (req size = 128)",
            "parameter_label": "0.1",
            "normalized_latency": 1.234,
        },
        {
            "condition_id": "size_8",
            "group": exp.READ_IMPACT_SIZE_SCAN_GROUP,
            "group_label": "Req size (insertion ratio = 0.2)",
            "parameter_label": "8",
            "normalized_latency": 2.0,
        },
    ]

    chart_path = exp.write_read_impact_grouped_bar_chart(
        rows,
        tmp_path / "read_impact_normalized_latency.svg",
    )
    svg_text = chart_path.read_text(encoding="utf-8")

    assert "Normalized latency" in svg_text
    assert "Control" in svg_text
    assert "Average READ Latency under READ/CIM Resource competetion" in svg_text
    assert "Insertion ratio (req size = 128)" in svg_text
    assert "Req size (insertion ratio = 0.2)" in svg_text
    assert exp.READ_IMPACT_GROUP_COLORS[exp.READ_IMPACT_BASELINE_GROUP] in svg_text
    assert exp.READ_IMPACT_GROUP_COLORS[exp.READ_IMPACT_RATIO_SCAN_GROUP] in svg_text
    assert exp.READ_IMPACT_GROUP_COLORS[exp.READ_IMPACT_SIZE_SCAN_GROUP] in svg_text
    assert 'data-bar-gap="12"' in svg_text
    assert 'data-group-gap="58"' in svg_text
    assert 'data-edge-padding="36"' in svg_text
    assert 'data-group="baseline" x="100.0"' in svg_text
    assert ">1.00<" in svg_text
    assert ">1.23<" in svg_text
    assert ">1.234<" not in svg_text


def test_read_impact_report_validation_rejects_mapping_read_counts():
    baseline_report = {
        "requests": [
            {
                "type": "READ",
                "scheduled_time": 1,
                "lha_start": 64,
                "size": 1,
                "host_completion_time": 20,
                "mapping_resolution_counts": {
                    "cmt_hit": 0,
                    "gmt_hit": 0,
                    "mapping_read": 1,
                    "uncached_write": 0,
                },
            }
        ]
    }
    contended_report = {
        "requests": [
            {
                "type": "READ",
                "scheduled_time": 1,
                "lha_start": 64,
                "size": 1,
                "host_completion_time": 25,
                "mapping_resolution_counts": {
                    "cmt_hit": 1,
                    "gmt_hit": 0,
                    "mapping_read": 0,
                    "uncached_write": 0,
                },
            }
        ]
    }

    with pytest.raises(ValueError, match="CMT-hit validation failed"):
        exp.validate_read_impact_reports_cmt_hits(baseline_report, contended_report)


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
