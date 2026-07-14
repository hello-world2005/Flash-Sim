"""Run request-size and read-impact resource-contention experiments."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


if __package__ in (None, ""):
    _HERE = Path(__file__).resolve().parent
    _REPO_ROOT = _HERE.parent
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
else:
    _REPO_ROOT = Path(__file__).resolve().parents[1]


from flash_sim.common import (
    BLOCK_PER_PLANE,
    CHANNEL_NO,
    CHIP_PER_CHANNEL,
    CMT_SIZE,
    DIE_PER_CHIP,
    LPA_NO_PER_MAPPING_PAGE,
    PAGE_PER_BLOCK,
    PLANE_PER_DIE,
    SECTOR_PER_PAGE,
    SL_PER_BLOCK,
    SSL_PER_SL,
    STATIC_BASE_LHA,
    STATIC_CHIP_PER_CHANNEL,
)
from flash_sim.config import make_event_runtime_geometry
from flash_sim.engine import Engine


DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "output" / "request_resource_contention_experiments"
DEFAULT_PRE_DATA_PATH = _REPO_ROOT / "pre_data" / "precondition_data.json"
DEFAULT_SCAN_SIZES = (1, 2, 4, 8)
SIZE_SCAN_REQUEST_TYPES = ("compute", "search")
SIZE_SCAN_CHART_TITLES = {
    "compute": "CIM normalized latency",
    "search": "CAM normalized latency",
}
DEFAULT_READ_COUNT: int | None = None
DEFAULT_FIRST_READ_ISSUE_TIME_NS = 1
DEFAULT_READ_TIME_STEP_NS = 100
DEFAULT_READ_SIZE_SECTORS = SECTOR_PER_PAGE
DEFAULT_CONTENTION_COMPUTE_SIZE = 8
READ_IMPACT_RATIO_SCAN_VALUES = (0.1, 0.2, 0.4, 0.8)
READ_IMPACT_SIZE_SCAN_VALUES = (8, 32, 128, 512)
READ_IMPACT_FIXED_SIZE_SCAN_RATIO = 0.2
READ_IMPACT_RATIO_SCAN_COMPUTE_SIZE = 128
READ_IMPACT_BASELINE_GROUP = "baseline"
READ_IMPACT_RATIO_SCAN_GROUP = "ratio_scan"
READ_IMPACT_SIZE_SCAN_GROUP = "size_scan"
READ_IMPACT_GROUP_LABELS = {
    READ_IMPACT_BASELINE_GROUP: "Control",
    READ_IMPACT_RATIO_SCAN_GROUP: "Insertion ratio (req size = 128)",
    READ_IMPACT_SIZE_SCAN_GROUP: "Req size (insertion ratio = 0.2)",
}
READ_IMPACT_GROUP_COLORS = {
    READ_IMPACT_BASELINE_GROUP: "#1f77b4",
    READ_IMPACT_RATIO_SCAN_GROUP: "#ff7f0e",
    READ_IMPACT_SIZE_SCAN_GROUP: "#9467bd",
}


@dataclass(frozen=True)
class SimulationResult:
    trace_path: Path
    report_path: Path
    report: dict[str, Any]


@dataclass(frozen=True)
class ReadImpactTracePlan:
    condition_id: str
    group: str
    parameter_label: str
    parameter_value: str | int | float
    configured_ratio: float | None
    compute_size: int | None
    num_read_req: int
    num_compute_req: int
    commands: list[dict[str, Any]]


def normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def static_region_end_exclusive() -> int:
    return STATIC_BASE_LHA + (
        CHANNEL_NO
        * STATIC_CHIP_PER_CHANNEL
        * DIE_PER_CHIP
        * PLANE_PER_DIE
        * BLOCK_PER_PLANE
        * SL_PER_BLOCK
        * SSL_PER_SL
    )


def validate_scan_sizes(sizes: Iterable[int]) -> tuple[int, ...]:
    validated: list[int] = []
    for size in sizes:
        try:
            numeric_size = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"scan size must be an integer: {size!r}") from exc
        if numeric_size <= 0:
            raise ValueError(f"scan size must be positive: {numeric_size}")
        if numeric_size > static_region_end_exclusive() - STATIC_BASE_LHA:
            raise ValueError(f"scan size exceeds static region capacity: {numeric_size}")
        validated.append(numeric_size)
    if not validated:
        raise ValueError("at least one scan size is required")
    return tuple(validated)


def _validate_static_request_type(request_type: str) -> str:
    normalized = str(request_type).lower()
    if normalized not in SIZE_SCAN_REQUEST_TYPES:
        raise ValueError(f"request type must be one of {SIZE_SCAN_REQUEST_TYPES}: {request_type!r}")
    return normalized


def static_start_lha_for_size(size: int, slot: int = 0) -> int:
    size = validate_scan_sizes([size])[0]
    slot = max(0, int(slot))
    capacity = static_region_end_exclusive() - STATIC_BASE_LHA
    offset = min(slot * size, capacity - size)
    return STATIC_BASE_LHA + offset


def build_static_request_command(
    request_type: str,
    size: int,
    *,
    time_ns: int = 0,
    slot: int = 0,
) -> dict[str, int | str]:
    normalized_type = _validate_static_request_type(request_type)
    numeric_size = validate_scan_sizes([size])[0]
    command = {
        "type": normalized_type,
        "time": int(time_ns),
        "start_lha": static_start_lha_for_size(numeric_size, slot=slot),
        "size": numeric_size,
    }
    if normalized_type == "compute":
        command["selected_wl"] = 0
    return command


def plan_size_scan_traces(
    sizes: Iterable[int],
    request_types: Iterable[str] = SIZE_SCAN_REQUEST_TYPES,
) -> list[dict[str, Any]]:
    validated_sizes = validate_scan_sizes(sizes)
    plans: list[dict[str, Any]] = []
    for request_type in request_types:
        normalized_type = _validate_static_request_type(request_type)
        for index, size in enumerate(validated_sizes):
            plans.append(
                {
                    "request_type": normalized_type,
                    "size": size,
                    "commands": [build_static_request_command(normalized_type, size, slot=index)],
                }
            )
    return plans


def trace_filename(prefix: str, *parts: object) -> str:
    clean_parts = [str(part).replace(" ", "_").replace("/", "_").replace("\\", "_") for part in parts]
    return "_".join([prefix, *clean_parts]) + ".json"


def write_trace(commands: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(commands, indent=2), encoding="utf-8")
    return path


def write_single_request_trace(
    output_root: str | Path,
    request_type: str,
    size: int,
    commands: list[dict[str, Any]] | None = None,
) -> Path:
    request_type = _validate_static_request_type(request_type)
    size = validate_scan_sizes([size])[0]
    if commands is None:
        commands = [build_static_request_command(request_type, size)]
    trace_path = normalize_path(output_root) / "traces" / "size_scan" / trace_filename(
        "size_scan",
        request_type,
        size,
    )
    return write_trace(commands, trace_path)


def run_engine_and_load_report(
    trace_path: str | Path,
    *,
    engine_factory: Callable[[], Any] = Engine,
    pre_trace: str | Path | None = None,
    quiet: bool = True,
) -> SimulationResult:
    trace_path = normalize_path(trace_path)
    engine = engine_factory()

    if quiet:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            engine.Start_simulation(str(trace_path), pre_trace=str(pre_trace) if pre_trace else None)
    else:
        engine.Start_simulation(str(trace_path), pre_trace=str(pre_trace) if pre_trace else None)

    report_path = getattr(engine, "last_request_latency_report_path", None)
    if report_path is None:
        raise RuntimeError(f"simulation did not expose a request latency report path for {trace_path}")
    report_path = normalize_path(report_path)
    if not report_path.exists():
        raise FileNotFoundError(f"request latency report was not written: {report_path}")
    return SimulationResult(
        trace_path=trace_path,
        report_path=report_path,
        report=json.loads(report_path.read_text(encoding="utf-8")),
    )


def aggregate_size_scan_result(
    request_type: str,
    size: int,
    simulation: SimulationResult,
) -> dict[str, Any]:
    requests = simulation.report.get("requests", [])
    if len(requests) != 1:
        raise ValueError(f"expected exactly one request in size-scan report, got {len(requests)}")
    request = requests[0]
    expected_type = _validate_static_request_type(request_type).upper()
    if request.get("type") != expected_type:
        raise ValueError(f"expected report request type {expected_type}, got {request.get('type')}")
    if int(request.get("size", -1)) != int(size):
        raise ValueError(f"expected report request size {size}, got {request.get('size')}")
    return {
        "request_type": expected_type.lower(),
        "size": int(size),
        "trace_path": str(simulation.trace_path),
        "report_path": str(simulation.report_path),
        "host_completion_time": int(request.get("host_completion_time") or 0),
        "total_latency": int(request.get("total_latency") or 0),
    }


def normalize_size_scan_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_by_type: dict[str, int] = {}
    for row in rows:
        request_type = str(row["request_type"]).lower()
        max_by_type[request_type] = max(max_by_type.get(request_type, 0), int(row["total_latency"]))

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        request_type = str(row["request_type"]).lower()
        total_latency = int(row["total_latency"])
        max_latency = max_by_type.get(request_type, 0)
        normalized = 0.0 if max_latency <= 0 else total_latency / max_latency
        normalized_row = dict(row)
        normalized_row["normalized_latency"] = normalized
        normalized_rows.append(normalized_row)
    return normalized_rows


def write_size_scan_json(rows: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "normalization": "per_request_type_max_total_latency",
        "results": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_size_scan_csv(rows: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "request_type",
        "size",
        "host_completion_time",
        "total_latency",
        "normalized_latency",
        "trace_path",
        "report_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return path


def write_svg_bar_chart(
    rows: list[dict[str, Any]],
    request_type: str,
    output_path: str | Path,
) -> Path:
    request_type = _validate_static_request_type(request_type)
    selected = sorted(
        (row for row in rows if str(row["request_type"]).lower() == request_type),
        key=lambda row: int(row["size"]),
    )
    if not selected:
        raise ValueError(f"no rows available for {request_type} chart")
    chart_title = SIZE_SCAN_CHART_TITLES[request_type]

    width = max(480, 120 + len(selected) * 80)
    height = 320
    margin_left = 56
    margin_right = 24
    margin_top = 40
    margin_bottom = 64
    chart_width = width - margin_left - margin_right
    chart_height = height - margin_top - margin_bottom
    bar_gap = 18
    bar_width = max(24, (chart_width - bar_gap * (len(selected) + 1)) / len(selected))

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2:.1f}" y="24" text-anchor="middle" font-family="Arial" font-size="16">{html.escape(chart_title)}</text>',
        f'<text x="18" y="{margin_top + chart_height / 2:.1f}" transform="rotate(-90 18 {margin_top + chart_height / 2:.1f})" text-anchor="middle" font-family="Arial" font-size="12">Normalized latency</text>',
        f'<text x="{margin_left + chart_width / 2:.1f}" y="{height - 18}" text-anchor="middle" font-family="Arial" font-size="12">Size</text>',
    ]

    for index, row in enumerate(selected):
        value = float(row.get("normalized_latency", 0.0))
        value = max(0.0, min(1.0, value))
        bar_height = chart_height * value
        x = margin_left + bar_gap + index * (bar_width + bar_gap)
        y = margin_top + chart_height - bar_height
        size_label = html.escape(str(row["size"]))
        value_label = f"{value:.2f}"
        elements.extend(
            [
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#2f6f8f"/>',
                f'<text x="{x + bar_width / 2:.1f}" y="{max(margin_top + 14, y - 6):.1f}" text-anchor="middle" font-family="Arial" font-size="11">{value_label}</text>',
                f'<text x="{x + bar_width / 2:.1f}" y="{height - margin_bottom + 20}" text-anchor="middle" font-family="Arial" font-size="12">{size_label}</text>',
            ]
        )

    elements.append(
        f'<rect x="{margin_left}" y="{margin_top}" width="{chart_width}" height="{chart_height}" fill="none" stroke="#333"/>'
    )
    elements.append("</svg>")
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(elements), encoding="utf-8")
    return path


def run_size_scan(
    sizes: Iterable[int] = DEFAULT_SCAN_SIZES,
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    engine_factory: Callable[[], Any] = Engine,
    simulate: Callable[..., SimulationResult] = run_engine_and_load_report,
) -> dict[str, Any]:
    output_root = normalize_path(output_root)
    plans = plan_size_scan_traces(sizes)
    raw_rows: list[dict[str, Any]] = []
    trace_paths: list[Path] = []

    for plan in plans:
        trace_path = write_single_request_trace(
            output_root,
            plan["request_type"],
            plan["size"],
            plan["commands"],
        )
        trace_paths.append(trace_path)
        simulation = simulate(trace_path, engine_factory=engine_factory)
        raw_rows.append(aggregate_size_scan_result(plan["request_type"], plan["size"], simulation))

    rows = normalize_size_scan_results(raw_rows)
    results_dir = output_root / "results"
    charts_dir = output_root / "charts"
    json_path = write_size_scan_json(rows, results_dir / "size_scan_results.json")
    csv_path = write_size_scan_csv(rows, results_dir / "size_scan_results.csv")
    chart_paths = {
        request_type: write_svg_bar_chart(
            rows,
            request_type,
            charts_dir / f"size_scan_{request_type}_normalized_latency.svg",
        )
        for request_type in SIZE_SCAN_REQUEST_TYPES
    }
    return {
        "output_root": str(output_root),
        "trace_paths": [str(path) for path in trace_paths],
        "results_json": str(json_path),
        "results_csv": str(csv_path),
        "chart_paths": {key: str(path) for key, path in chart_paths.items()},
        "results": rows,
        "simulation_count": len(plans),
    }


def valid_sector_runs(valid_bitmap: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, bit in enumerate(valid_bitmap):
        if bit and start is None:
            start = index
        elif not bit and start is not None:
            runs.append((start, index - start))
            start = None
    if start is not None:
        runs.append((start, len(valid_bitmap) - start))
    return runs


def load_precondition_records(pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH) -> list[dict[str, Any]]:
    path = normalize_path(pre_data_path)
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_sector_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted((start, start + length) for start, length in ranges)
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end - start) for start, end in merged]


def read_lpa_sector_ranges(read_commands: list[dict[str, Any]]) -> dict[int, list[tuple[int, int]]]:
    touched: dict[int, list[tuple[int, int]]] = {}
    for command in read_commands:
        if str(command.get("type", "")).lower() != "read":
            raise ValueError("read-impact CMT-hit validation only accepts read commands")
        start_lha = int(command.get("start_lha", -1))
        size = int(command.get("size", 0))
        if start_lha < 0:
            raise ValueError(f"read start_lha must be non-negative: {start_lha}")
        if size <= 0:
            raise ValueError(f"read size must be positive: {size}")

        current_lha = start_lha
        remaining = size
        while remaining > 0:
            lpa = current_lha // SECTOR_PER_PAGE
            sector_offset = current_lha % SECTOR_PER_PAGE
            length = min(remaining, SECTOR_PER_PAGE - sector_offset)
            touched.setdefault(lpa, []).append((sector_offset, length))
            current_lha += length
            remaining -= length

    return {lpa: _merge_sector_ranges(ranges) for lpa, ranges in touched.items()}


def read_impact_cmt_warm_capacity() -> int:
    geometry = make_event_runtime_geometry()
    cmt_ratio = getattr(geometry, "preconditioning_cmt_ratio", 0.5)
    if not (0.0 < cmt_ratio <= 1.0):
        cmt_ratio = 0.5
    return int(CMT_SIZE * cmt_ratio)


def read_impact_random_access_data_pages() -> int:
    geometry = make_event_runtime_geometry()
    non_static_chip_no = geometry.chip_per_channel - geometry.static_chip_per_channel
    total_random_access_pages = (
        geometry.channel_no
        * non_static_chip_no
        * geometry.dies
        * geometry.planes_per_die
        * geometry.blocks_per_plane
        * geometry.pages_per_block
    )
    mapping_page_count = (
        total_random_access_pages + LPA_NO_PER_MAPPING_PAGE - 1
    ) // LPA_NO_PER_MAPPING_PAGE
    return total_random_access_pages - mapping_page_count


def read_impact_lpa_chip(lpa: int) -> int:
    lpa = int(lpa)
    if lpa < 0 or lpa >= read_impact_random_access_data_pages():
        raise ValueError(
            f"LPA {lpa} out of random-access data range "
            f"[0, {read_impact_random_access_data_pages() - 1}]"
        )
    pages_per_plane = BLOCK_PER_PLANE * PAGE_PER_BLOCK
    rem = lpa // pages_per_plane
    rem //= PLANE_PER_DIE
    rem //= DIE_PER_CHIP
    return rem % CHIP_PER_CHANNEL


def read_impact_lpa_is_preconditionable(lpa: int) -> bool:
    try:
        chip = read_impact_lpa_chip(lpa)
    except ValueError:
        return False
    return chip < CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL


def validate_read_impact_page_reads(read_commands: list[dict[str, Any]]) -> None:
    for command in read_commands:
        if str(command.get("type", "")).lower() != "read":
            raise ValueError("read-impact CMT-hit validation only accepts read commands")
        start_lha = int(command.get("start_lha", -1))
        size = int(command.get("size", 0))
        if start_lha < 0:
            raise ValueError(f"read start_lha must be non-negative: {start_lha}")
        if start_lha % SECTOR_PER_PAGE != 0 or size != SECTOR_PER_PAGE:
            raise ValueError(
                "read-impact CMT-hit validation failed: "
                f"read must be a page-aligned page read, got start_lha={start_lha}, size={size}"
            )


def validate_read_commands_for_cmt_hit(
    read_commands: list[dict[str, Any]],
    precondition_records: list[dict[str, Any]],
    *,
    cmt_capacity: int | None = None,
) -> list[dict[str, Any]]:
    validate_read_impact_page_reads(read_commands)
    touched = read_lpa_sector_ranges(read_commands)
    capacity = read_impact_cmt_warm_capacity() if cmt_capacity is None else int(cmt_capacity)
    if len(touched) > capacity:
        raise ValueError(
            "read-impact CMT-hit validation failed: "
            f"{len(touched)} touched LPAs exceed CMT warm capacity {capacity}"
        )

    records_by_lpa = {int(record["lpa"]): record for record in precondition_records}
    for lpa, ranges in touched.items():
        if not read_impact_lpa_is_preconditionable(lpa):
            raise ValueError(
                "read-impact CMT-hit validation failed: "
                f"LPA {lpa} is not preconditionable for CMT warm-up"
            )
        record = records_by_lpa.get(lpa)
        if record is None:
            raise ValueError(
                f"read-impact CMT-hit validation failed: missing preconditioned LPA {lpa}"
            )
        valid_bitmap = [int(bit) for bit in record.get("valid_bitmap", [])]
        for sector_offset, length in ranges:
            end = sector_offset + length
            if end > len(valid_bitmap):
                raise ValueError(
                    "read-impact CMT-hit validation failed: "
                    f"LPA {lpa} sector range {sector_offset}:{end} exceeds valid bitmap"
                )
            if any(bit == 0 for bit in valid_bitmap[sector_offset:end]):
                raise ValueError(
                    "read-impact CMT-hit validation failed: "
                    f"LPA {lpa} has invalid sector in range {sector_offset}:{end}"
                )

    touched_lpas = set(touched)
    return [dict(record) for record in precondition_records if int(record["lpa"]) in touched_lpas]


def select_read_impact_precondition_records(
    read_commands: list[dict[str, Any]],
    *,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
) -> list[dict[str, Any]]:
    return validate_read_commands_for_cmt_hit(
        read_commands,
        load_precondition_records(pre_data_path),
    )


def write_read_impact_precondition(
    output_root: str | Path,
    records: list[dict[str, Any]],
) -> Path:
    path = normalize_path(output_root) / "traces" / "read_impact" / "read_impact_cmt_hit_precondition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return path


def _precondition_record_has_full_valid_page(record: dict[str, Any]) -> bool:
    if not read_impact_lpa_is_preconditionable(int(record["lpa"])):
        return False
    valid_bitmap = [int(bit) for bit in record.get("valid_bitmap", [])]
    return len(valid_bitmap) >= SECTOR_PER_PAGE and all(valid_bitmap[:SECTOR_PER_PAGE])


def build_default_read_commands(
    *,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_count: int | None = DEFAULT_READ_COUNT,
    first_issue_time_ns: int = DEFAULT_FIRST_READ_ISSUE_TIME_NS,
    time_step_ns: int = DEFAULT_READ_TIME_STEP_NS,
    read_size_sectors: int = DEFAULT_READ_SIZE_SECTORS,
) -> list[dict[str, int | str]]:
    if read_count is not None and read_count <= 0:
        raise ValueError("read_count must be positive")
    if int(read_size_sectors) != SECTOR_PER_PAGE:
        raise ValueError("read-impact default reads must target exactly one page")

    commands: list[dict[str, int | str]] = []
    target_count = read_impact_cmt_warm_capacity() if read_count is None else int(read_count)
    for record in load_precondition_records(pre_data_path):
        if not _precondition_record_has_full_valid_page(record):
            continue
        lpa = int(record["lpa"])
        commands.append(
            {
                "type": "read",
                "time": int(first_issue_time_ns) + len(commands) * int(time_step_ns),
                "start_lha": lpa * SECTOR_PER_PAGE,
                "size": SECTOR_PER_PAGE,
            }
        )
        if len(commands) >= target_count:
            return commands
    if commands and read_count is None:
        return commands
    raise ValueError(f"not enough full-page preconditioned records for {target_count} read requests")


def read_commands_only(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(command) for command in commands if str(command.get("type")).lower() == "read"]


def build_compute_prefix_commands(
    *,
    compute_size: int = DEFAULT_CONTENTION_COMPUTE_SIZE,
) -> list[dict[str, int | str]]:
    compute_size = validate_scan_sizes([compute_size])[0]
    return [
        build_static_request_command("compute", compute_size, time_ns=0, slot=0),
        build_static_request_command("compute", compute_size, time_ns=0, slot=1),
    ]


def _format_read_impact_ratio(ratio: float) -> str:
    return f"{float(ratio):g}"


def _read_impact_condition_id(prefix: str, value: object) -> str:
    return f"{prefix}_{str(value).replace('.', '_')}"


def read_impact_compute_count(num_read_req: int, ratio: float) -> int:
    num_read_req = int(num_read_req)
    ratio = float(ratio)
    if num_read_req <= 0:
        raise ValueError("num_read_req must be positive")
    if ratio <= 0:
        raise ValueError("read-impact compute insertion ratio must be positive")
    return max(1, round(num_read_req * ratio))


def read_impact_insertion_anchors(num_read_req: int, num_compute_req: int) -> list[int]:
    num_read_req = int(num_read_req)
    num_compute_req = int(num_compute_req)
    if num_read_req <= 0:
        raise ValueError("num_read_req must be positive")
    if num_compute_req <= 0:
        raise ValueError("num_compute_req must be positive")
    return [
        min(num_read_req - 1, ((index + 1) * num_read_req) // (num_compute_req + 1))
        for index in range(num_compute_req)
    ]


def insert_compute_commands_for_read_impact(
    read_commands: list[dict[str, Any]],
    *,
    ratio: float,
    compute_size: int,
) -> tuple[list[dict[str, Any]], int]:
    validate_read_impact_page_reads(read_commands)
    compute_size = validate_scan_sizes([compute_size])[0]
    compute_count = read_impact_compute_count(len(read_commands), ratio)
    anchor_counts: dict[int, int] = {}
    for anchor in read_impact_insertion_anchors(len(read_commands), compute_count):
        anchor_counts[anchor] = anchor_counts.get(anchor, 0) + 1

    commands: list[dict[str, Any]] = []
    compute_slot = 0
    for read_index, read_command in enumerate(read_commands):
        for _ in range(anchor_counts.get(read_index, 0)):
            commands.append(
                build_static_request_command(
                    "compute",
                    compute_size,
                    time_ns=int(read_command["time"]),
                    slot=compute_slot,
                )
            )
            compute_slot += 1
        commands.append(dict(read_command))
    if read_commands_only(commands) != [dict(command) for command in read_commands]:
        raise ValueError("contention trace read portion does not match baseline")
    return commands, compute_count


def _copy_read_commands_for_impact(
    *,
    read_commands: list[dict[str, Any]] | None,
    pre_data_path: str | Path,
    read_count: int | None,
) -> list[dict[str, Any]]:
    baseline = [
        dict(command)
        for command in (
            read_commands
            if read_commands is not None
            else build_default_read_commands(pre_data_path=pre_data_path, read_count=read_count)
        )
    ]
    if not baseline:
        raise ValueError("at least one read command is required")
    validate_read_impact_page_reads(baseline)
    return baseline


def build_read_impact_trace_plans(
    *,
    read_commands: list[dict[str, Any]] | None = None,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_count: int | None = DEFAULT_READ_COUNT,
    ratio_scan_values: Iterable[float] = READ_IMPACT_RATIO_SCAN_VALUES,
    size_scan_values: Iterable[int] = READ_IMPACT_SIZE_SCAN_VALUES,
) -> list[ReadImpactTracePlan]:
    baseline = _copy_read_commands_for_impact(
        read_commands=read_commands,
        pre_data_path=pre_data_path,
        read_count=read_count,
    )
    plans = [
        ReadImpactTracePlan(
            condition_id="baseline",
            group=READ_IMPACT_BASELINE_GROUP,
            parameter_label="control",
            parameter_value="control",
            configured_ratio=None,
            compute_size=None,
            num_read_req=len(baseline),
            num_compute_req=0,
            commands=[dict(command) for command in baseline],
        )
    ]

    for ratio in ratio_scan_values:
        ratio_label = _format_read_impact_ratio(float(ratio))
        commands, compute_count = insert_compute_commands_for_read_impact(
            baseline,
            ratio=float(ratio),
            compute_size=READ_IMPACT_RATIO_SCAN_COMPUTE_SIZE,
        )
        plans.append(
            ReadImpactTracePlan(
                condition_id=_read_impact_condition_id("ratio", ratio_label),
                group=READ_IMPACT_RATIO_SCAN_GROUP,
                parameter_label=ratio_label,
                parameter_value=float(ratio),
                configured_ratio=float(ratio),
                compute_size=READ_IMPACT_RATIO_SCAN_COMPUTE_SIZE,
                num_read_req=len(baseline),
                num_compute_req=compute_count,
                commands=commands,
            )
        )

    for compute_size in validate_scan_sizes(size_scan_values):
        commands, compute_count = insert_compute_commands_for_read_impact(
            baseline,
            ratio=READ_IMPACT_FIXED_SIZE_SCAN_RATIO,
            compute_size=compute_size,
        )
        plans.append(
            ReadImpactTracePlan(
                condition_id=_read_impact_condition_id("size", compute_size),
                group=READ_IMPACT_SIZE_SCAN_GROUP,
                parameter_label=str(compute_size),
                parameter_value=compute_size,
                configured_ratio=READ_IMPACT_FIXED_SIZE_SCAN_RATIO,
                compute_size=compute_size,
                num_read_req=len(baseline),
                num_compute_req=compute_count,
                commands=commands,
            )
        )

    return plans


def build_paired_read_impact_traces(
    *,
    read_commands: list[dict[str, Any]] | None = None,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_count: int | None = DEFAULT_READ_COUNT,
    compute_size: int = DEFAULT_CONTENTION_COMPUTE_SIZE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline = _copy_read_commands_for_impact(
        read_commands=read_commands,
        pre_data_path=pre_data_path,
        read_count=read_count,
    )
    contended, _ = insert_compute_commands_for_read_impact(
        baseline,
        ratio=READ_IMPACT_FIXED_SIZE_SCAN_RATIO,
        compute_size=compute_size,
    )
    return baseline, contended


def _read_impact_trace_filename(plan: ReadImpactTracePlan) -> str:
    if plan.group == READ_IMPACT_BASELINE_GROUP:
        return "read_impact_baseline.json"
    return trace_filename("read_impact", plan.condition_id)


def write_read_impact_trace_plans(
    output_root: str | Path,
    plans: list[ReadImpactTracePlan],
) -> dict[str, Path]:
    output_root = normalize_path(output_root)
    trace_dir = output_root / "traces" / "read_impact"
    return {
        plan.condition_id: write_trace(plan.commands, trace_dir / _read_impact_trace_filename(plan))
        for plan in plans
    }


def write_read_impact_traces(
    output_root: str | Path,
    baseline_commands: list[dict[str, Any]],
    contended_commands: list[dict[str, Any]],
) -> tuple[Path, Path]:
    output_root = normalize_path(output_root)
    trace_dir = output_root / "traces" / "read_impact"
    baseline_path = write_trace(baseline_commands, trace_dir / "read_impact_baseline.json")
    contended_path = write_trace(contended_commands, trace_dir / "read_impact_compute_contention.json")
    return baseline_path, contended_path


def _report_issue_time(request: dict[str, Any]) -> int:
    for key in ("scheduled_time", "trace_time", "req_init_time"):
        value = request.get(key)
        if value is not None:
            return int(value)
    return 0


def read_identity_from_report_request(request: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        str(request.get("type", "")).lower(),
        _report_issue_time(request),
        int(request.get("lha_start") or 0),
        int(request.get("size") or 0),
    )


def _read_request_map(report: dict[str, Any]) -> dict[tuple[str, int, int, int], dict[str, Any]]:
    rows: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for request in report.get("requests", []):
        if str(request.get("type", "")).upper() != "READ":
            continue
        identity = read_identity_from_report_request(request)
        if identity in rows:
            raise ValueError(f"duplicate read identity in report: {identity}")
        rows[identity] = request
    return rows


def validate_compute_prefix_overlap(
    contended_report: dict[str, Any],
    *,
    first_read_issue_time: int,
) -> None:
    requests = contended_report.get("requests", [])
    if len(requests) < 3:
        raise ValueError("compute-contention report must contain two compute requests and at least one read")
    prefix = requests[:2]
    if any(str(request.get("type", "")).upper() != "COMPUTE" for request in prefix):
        raise ValueError("compute-contention report does not begin with two compute requests")
    for request in prefix:
        completion_time = request.get("host_completion_time")
        if completion_time is None or int(completion_time) <= int(first_read_issue_time):
            raise ValueError(
                "prepended compute request did not complete after the first read issue time"
            )


def _mapping_resolution_counts_for_read(
    request: dict[str, Any],
    *,
    label: str,
) -> dict[str, int]:
    identity = read_identity_from_report_request(request)
    raw_counts = request.get("mapping_resolution_counts")
    if not isinstance(raw_counts, dict):
        raise ValueError(
            f"read-impact CMT-hit validation failed for {label} read {identity}: "
            "missing mapping_resolution_counts"
        )
    required_keys = ("cmt_hit", "gmt_hit", "mapping_read", "uncached_write")
    missing = [key for key in required_keys if key not in raw_counts]
    if missing:
        raise ValueError(
            f"read-impact CMT-hit validation failed for {label} read {identity}: "
            f"missing mapping count keys {missing}"
        )
    return {key: int(raw_counts[key]) for key in required_keys}


def validate_read_report_cmt_hits(
    report: dict[str, Any],
    *,
    label: str,
) -> None:
    reads = _read_request_map(report)
    for identity, request in reads.items():
        counts = _mapping_resolution_counts_for_read(request, label=label)
        total_lookups = sum(counts.values())
        if (
            total_lookups <= 0
            or counts["mapping_read"] != 0
            or counts["cmt_hit"] != total_lookups
        ):
            raise ValueError(
                f"read-impact CMT-hit validation failed for {label} read {identity}: "
                f"mapping_resolution_counts={counts}"
            )


def validate_read_impact_reports_cmt_hits(
    baseline_report: dict[str, Any],
    contended_report: dict[str, Any],
) -> None:
    baseline_reads = _read_request_map(baseline_report)
    contended_reads = _read_request_map(contended_report)
    if set(baseline_reads) != set(contended_reads):
        missing = sorted(set(baseline_reads) - set(contended_reads))
        extra = sorted(set(contended_reads) - set(baseline_reads))
        raise ValueError(f"read identity mismatch: missing={missing}, extra={extra}")
    validate_read_report_cmt_hits(baseline_report, label="baseline")
    validate_read_report_cmt_hits(contended_report, label="compute-contention")


def validate_read_impact_scan_reports_cmt_hits(
    baseline_report: dict[str, Any],
    condition_reports: dict[str, dict[str, Any]],
) -> None:
    baseline_reads = _read_request_map(baseline_report)
    validate_read_report_cmt_hits(baseline_report, label="baseline")
    for condition_id, report in condition_reports.items():
        condition_reads = _read_request_map(report)
        if set(baseline_reads) != set(condition_reads):
            missing = sorted(set(baseline_reads) - set(condition_reads))
            extra = sorted(set(condition_reads) - set(baseline_reads))
            raise ValueError(
                f"read identity mismatch for {condition_id}: missing={missing}, extra={extra}"
            )
        validate_read_report_cmt_hits(report, label=condition_id)


def compare_read_completion_times(
    baseline_report: dict[str, Any],
    contended_report: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline_reads = _read_request_map(baseline_report)
    contended_reads = _read_request_map(contended_report)
    if set(baseline_reads) != set(contended_reads):
        missing = sorted(set(baseline_reads) - set(contended_reads))
        extra = sorted(set(contended_reads) - set(baseline_reads))
        raise ValueError(f"read identity mismatch: missing={missing}, extra={extra}")

    rows: list[dict[str, Any]] = []
    for identity in sorted(baseline_reads, key=lambda item: (item[1], item[2], item[3])):
        baseline_request = baseline_reads[identity]
        contended_request = contended_reads[identity]
        baseline_completion = int(baseline_request.get("host_completion_time") or 0)
        contended_completion = int(contended_request.get("host_completion_time") or 0)
        rows.append(
            {
                "type": identity[0],
                "time": identity[1],
                "start_lha": identity[2],
                "size": identity[3],
                "baseline_host_completion_time": baseline_completion,
                "contended_host_completion_time": contended_completion,
                "completion_time_delta": contended_completion - baseline_completion,
            }
        )
    return rows


def _request_latency_value(request: dict[str, Any]) -> float:
    total_latency = request.get("total_latency")
    if total_latency is not None:
        return float(total_latency)
    completion_time = request.get("host_completion_time")
    if completion_time is None:
        raise ValueError(f"READ request missing total_latency and host_completion_time: {request}")
    return float(int(completion_time) - _report_issue_time(request))


def average_read_latency(report: dict[str, Any]) -> float:
    reads = list(_read_request_map(report).values())
    if not reads:
        raise ValueError("read-impact report contains no READ requests")
    return sum(_request_latency_value(request) for request in reads) / len(reads)


def build_read_impact_result_rows(
    plans: list[ReadImpactTracePlan],
    simulations: dict[str, SimulationResult],
) -> list[dict[str, Any]]:
    if not plans or plans[0].group != READ_IMPACT_BASELINE_GROUP:
        raise ValueError("read-impact result rows require the first plan to be the baseline")
    baseline_simulation = simulations[plans[0].condition_id]
    baseline_average = average_read_latency(baseline_simulation.report)

    rows: list[dict[str, Any]] = []
    for plan in plans:
        simulation = simulations[plan.condition_id]
        average_latency = average_read_latency(simulation.report)
        normalized_latency = (
            1.0
            if plan.group == READ_IMPACT_BASELINE_GROUP
            else (0.0 if baseline_average <= 0 else average_latency / baseline_average)
        )
        rows.append(
            {
                "condition_id": plan.condition_id,
                "group": plan.group,
                "group_label": READ_IMPACT_GROUP_LABELS[plan.group],
                "parameter_label": plan.parameter_label,
                "parameter_value": plan.parameter_value,
                "configured_ratio": "" if plan.configured_ratio is None else plan.configured_ratio,
                "compute_size": "" if plan.compute_size is None else plan.compute_size,
                "num_read_req": plan.num_read_req,
                "num_compute_req": plan.num_compute_req,
                "average_read_latency": average_latency,
                "normalized_latency": normalized_latency,
                "trace_path": str(simulation.trace_path),
                "report_path": str(simulation.report_path),
            }
        )
    return rows


def write_read_impact_json(
    rows: list[dict[str, Any]],
    output_path: str | Path,
    *,
    baseline_trace_path: str | Path,
    trace_paths: dict[str, str | Path],
    cmt_hit_precondition_path: str | Path,
) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "normalization": "baseline_average_read_latency",
        "baseline_trace_path": str(normalize_path(baseline_trace_path)),
        "cmt_hit_precondition_path": str(normalize_path(cmt_hit_precondition_path)),
        "trace_paths": {
            condition_id: str(normalize_path(trace_path))
            for condition_id, trace_path in trace_paths.items()
        },
        "results": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_read_impact_csv(rows: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "condition_id",
        "group",
        "group_label",
        "parameter_label",
        "parameter_value",
        "configured_ratio",
        "compute_size",
        "num_read_req",
        "num_compute_req",
        "average_read_latency",
        "normalized_latency",
        "trace_path",
        "report_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return path


def write_read_impact_grouped_bar_chart(
    rows: list[dict[str, Any]],
    output_path: str | Path,
) -> Path:
    group_order = [
        READ_IMPACT_BASELINE_GROUP,
        READ_IMPACT_RATIO_SCAN_GROUP,
        READ_IMPACT_SIZE_SCAN_GROUP,
    ]
    rows_by_group = {
        group: [row for row in rows if row.get("group") == group]
        for group in group_order
    }
    if any(not rows_by_group[group] for group in group_order):
        raise ValueError("read-impact chart requires baseline, ratio-scan, and size-scan rows")

    margin_left = 64
    margin_right = 28
    margin_top = 44
    margin_bottom = 96
    chart_height = 220
    bar_width = 42
    bar_gap = 12
    group_gap = 58
    edge_padding = 36
    total_bars = sum(len(rows_by_group[group]) for group in group_order)
    total_inner_gaps = sum(max(0, len(rows_by_group[group]) - 1) for group in group_order)
    chart_width = (
        edge_padding * 2
        + total_bars * bar_width
        + total_inner_gaps * bar_gap
        + (len(group_order) - 1) * group_gap
    )
    width = margin_left + chart_width + margin_right
    height = margin_top + chart_height + margin_bottom
    max_value = max(1.0, *(float(row["normalized_latency"]) for row in rows))
    y_scale_max = max_value * 1.15

    elements = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" data-bar-gap="{bar_gap}" '
            f'data-group-gap="{group_gap}" data-edge-padding="{edge_padding}">'
        ),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{width / 2:.1f}" y="24" text-anchor="middle" font-family="Arial" font-size="16">Average READ Latency under READ/CIM Resource competetion</text>',
        f'<text x="18" y="{margin_top + chart_height / 2:.1f}" transform="rotate(-90 18 {margin_top + chart_height / 2:.1f})" text-anchor="middle" font-family="Arial" font-size="12">Normalized latency</text>',
    ]

    x = margin_left + edge_padding
    group_centers: dict[str, float] = {}
    for group in group_order:
        group_rows = rows_by_group[group]
        group_start = x
        fill = READ_IMPACT_GROUP_COLORS[group]
        for row in group_rows:
            value = float(row["normalized_latency"])
            bar_height = 0.0 if y_scale_max <= 0 else chart_height * (value / y_scale_max)
            y = margin_top + chart_height - bar_height
            parameter_label = html.escape(str(row["parameter_label"]))
            value_label = f"{value:.2f}"
            elements.extend(
                [
                    f'<rect data-group="{group}" x="{x:.1f}" y="{y:.1f}" width="{bar_width}" height="{bar_height:.1f}" fill="{fill}"/>',
                    f'<text x="{x + bar_width / 2:.1f}" y="{max(margin_top + 12, y - 6):.1f}" text-anchor="middle" font-family="Arial" font-size="11">{value_label}</text>',
                    f'<text x="{x + bar_width / 2:.1f}" y="{margin_top + chart_height + 22}" text-anchor="middle" font-family="Arial" font-size="12">{parameter_label}</text>',
                ]
            )
            x += bar_width + bar_gap
        group_end = x - bar_gap
        group_centers[group] = (group_start + group_end) / 2
        x += group_gap - bar_gap

    for group in group_order:
        elements.append(
            f'<text x="{group_centers[group]:.1f}" y="{height - 24}" text-anchor="middle" font-family="Arial" font-size="12">{html.escape(READ_IMPACT_GROUP_LABELS[group])}</text>'
        )
    elements.append(
        f'<rect x="{margin_left}" y="{margin_top}" width="{chart_width}" height="{chart_height}" fill="none" stroke="#333"/>'
    )
    elements.append("</svg>")
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(elements), encoding="utf-8")
    return path


def run_read_impact_comparison(
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_commands: list[dict[str, Any]] | None = None,
    read_count: int | None = DEFAULT_READ_COUNT,
    compute_size: int = DEFAULT_CONTENTION_COMPUTE_SIZE,
    engine_factory: Callable[[], Any] = Engine,
    simulate: Callable[..., SimulationResult] = run_engine_and_load_report,
) -> dict[str, Any]:
    output_root = normalize_path(output_root)
    plans = build_read_impact_trace_plans(
        read_commands=read_commands,
        pre_data_path=pre_data_path,
        read_count=read_count,
    )
    baseline_plan = plans[0]
    trace_paths = write_read_impact_trace_plans(output_root, plans)
    cmt_hit_precondition_records = select_read_impact_precondition_records(
        baseline_plan.commands,
        pre_data_path=pre_data_path,
    )
    cmt_hit_precondition_path = write_read_impact_precondition(
        output_root,
        cmt_hit_precondition_records,
    )
    simulations: dict[str, SimulationResult] = {}
    for plan in plans:
        simulations[plan.condition_id] = simulate(
            trace_paths[plan.condition_id],
            engine_factory=engine_factory,
            pre_trace=cmt_hit_precondition_path,
        )
    baseline_sim = simulations[baseline_plan.condition_id]
    condition_reports = {
        plan.condition_id: simulations[plan.condition_id].report
        for plan in plans
        if plan.group != READ_IMPACT_BASELINE_GROUP
    }
    validate_read_impact_scan_reports_cmt_hits(baseline_sim.report, condition_reports)
    rows = build_read_impact_result_rows(plans, simulations)

    results_dir = output_root / "results"
    charts_dir = output_root / "charts"
    json_path = write_read_impact_json(
        rows,
        results_dir / "read_impact_comparison.json",
        baseline_trace_path=trace_paths[baseline_plan.condition_id],
        trace_paths=trace_paths,
        cmt_hit_precondition_path=cmt_hit_precondition_path,
    )
    csv_path = write_read_impact_csv(rows, results_dir / "read_impact_comparison.csv")
    chart_path = write_read_impact_grouped_bar_chart(
        rows,
        charts_dir / "read_impact_normalized_latency.svg",
    )
    return {
        "output_root": str(output_root),
        "baseline_trace_path": str(trace_paths[baseline_plan.condition_id]),
        "trace_paths": {condition_id: str(path) for condition_id, path in trace_paths.items()},
        "cmt_hit_precondition_path": str(cmt_hit_precondition_path),
        "results_json": str(json_path),
        "results_csv": str(csv_path),
        "chart_path": str(chart_path),
        "results": rows,
        "simulation_count": len(plans),
    }


def parse_size_list(value: str) -> tuple[int, ...]:
    if not value:
        raise ValueError("size list cannot be empty")
    return validate_scan_sizes(part.strip() for part in value.split(",") if part.strip())


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment",
        choices=("all", "size-scan", "read-impact"),
        default="all",
        help="Experiment workflow to run.",
    )
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_SCAN_SIZES),
        help="Comma-separated request sizes for compute/search size scans.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory for generated traces, result tables, and charts.",
    )
    parser.add_argument(
        "--pre-data",
        type=Path,
        default=DEFAULT_PRE_DATA_PATH,
        help="precondition_data.json used to derive valid default read requests.",
    )
    parser.add_argument(
        "--read-count",
        type=int,
        default=DEFAULT_READ_COUNT,
        help="Number of default read requests in the read-impact experiment; omit to use all CMT-warmable page reads.",
    )
    parser.add_argument(
        "--compute-size",
        type=int,
        default=DEFAULT_CONTENTION_COMPUTE_SIZE,
        help="Deprecated for read-impact scans; ratio and request-size scans use fixed configured sizes.",
    )
    return parser.parse_args(argv)


def _print_size_scan_summary(result: dict[str, Any]) -> None:
    print("[size-scan] traces:")
    for path in result["trace_paths"]:
        print(f"  {path}")
    print(f"[size-scan] results JSON: {result['results_json']}")
    print(f"[size-scan] results CSV: {result['results_csv']}")
    for request_type, path in result["chart_paths"].items():
        print(f"[size-scan] {request_type} chart: {path}")


def _print_read_impact_summary(result: dict[str, Any]) -> None:
    print(f"[read-impact] baseline trace: {result['baseline_trace_path']}")
    print("[read-impact] scan traces:")
    for condition_id, path in result["trace_paths"].items():
        if condition_id == "baseline":
            continue
        print(f"  {condition_id}: {path}")
    print(f"[read-impact] CMT-hit precondition: {result['cmt_hit_precondition_path']}")
    print(f"[read-impact] comparison JSON: {result['results_json']}")
    print(f"[read-impact] comparison CSV: {result['results_csv']}")
    print(f"[read-impact] normalized latency chart: {result['chart_path']}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    sizes = parse_size_list(args.sizes)
    if args.experiment in ("all", "size-scan"):
        size_scan = run_size_scan(sizes, output_root=args.output_root)
        _print_size_scan_summary(size_scan)
    if args.experiment in ("all", "read-impact"):
        read_impact = run_read_impact_comparison(
            output_root=args.output_root,
            pre_data_path=args.pre_data,
            read_count=args.read_count,
            compute_size=args.compute_size,
        )
        _print_read_impact_summary(read_impact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
