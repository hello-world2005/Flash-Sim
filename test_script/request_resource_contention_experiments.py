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
    DIE_PER_CHIP,
    PLANE_PER_DIE,
    SECTOR_PER_PAGE,
    SL_PER_BLOCK,
    SSL_PER_SL,
    STATIC_BASE_LHA,
    STATIC_CHIP_PER_CHANNEL,
)
from flash_sim.engine import Engine


DEFAULT_OUTPUT_ROOT = _REPO_ROOT / "output" / "request_resource_contention_experiments"
DEFAULT_PRE_DATA_PATH = _REPO_ROOT / "pre_data" / "precondition_data.json"
DEFAULT_SCAN_SIZES = (1, 2, 4, 8)
SIZE_SCAN_REQUEST_TYPES = ("compute", "search")
DEFAULT_READ_COUNT = 3
DEFAULT_FIRST_READ_ISSUE_TIME_NS = 1
DEFAULT_READ_TIME_STEP_NS = 100
DEFAULT_READ_SIZE_SECTORS = 1
DEFAULT_CONTENTION_COMPUTE_SIZE = 8


@dataclass(frozen=True)
class SimulationResult:
    trace_path: Path
    report_path: Path
    report: dict[str, Any]


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
    return {
        "type": normalized_type,
        "time": int(time_ns),
        "start_lha": static_start_lha_for_size(numeric_size, slot=slot),
        "size": numeric_size,
    }


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
        f'<text x="{width / 2:.1f}" y="24" text-anchor="middle" font-family="Arial" font-size="16">{html.escape(request_type.upper())} normalized latency by size</text>',
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" stroke="#333"/>',
        f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" stroke="#333"/>',
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
        value_label = f"{value:.3f}".rstrip("0").rstrip(".")
        elements.extend(
            [
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="#2f6f8f"/>',
                f'<text x="{x + bar_width / 2:.1f}" y="{max(margin_top + 14, y - 6):.1f}" text-anchor="middle" font-family="Arial" font-size="11">{value_label}</text>',
                f'<text x="{x + bar_width / 2:.1f}" y="{height - margin_bottom + 20}" text-anchor="middle" font-family="Arial" font-size="12">{size_label}</text>',
            ]
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


def build_default_read_commands(
    *,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_count: int = DEFAULT_READ_COUNT,
    first_issue_time_ns: int = DEFAULT_FIRST_READ_ISSUE_TIME_NS,
    time_step_ns: int = DEFAULT_READ_TIME_STEP_NS,
    read_size_sectors: int = DEFAULT_READ_SIZE_SECTORS,
) -> list[dict[str, int | str]]:
    if read_count <= 0:
        raise ValueError("read_count must be positive")
    if read_size_sectors <= 0:
        raise ValueError("read_size_sectors must be positive")

    commands: list[dict[str, int | str]] = []
    for record in load_precondition_records(pre_data_path):
        lpa = int(record["lpa"])
        for sector_offset, run_length in valid_sector_runs(record.get("valid_bitmap", [])):
            size = min(int(read_size_sectors), run_length)
            if size <= 0:
                continue
            commands.append(
                {
                    "type": "read",
                    "time": int(first_issue_time_ns) + len(commands) * int(time_step_ns),
                    "start_lha": lpa * SECTOR_PER_PAGE + sector_offset,
                    "size": size,
                }
            )
            break
        if len(commands) >= read_count:
            return commands
    raise ValueError(f"not enough valid preconditioned records for {read_count} read requests")


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


def build_paired_read_impact_traces(
    *,
    read_commands: list[dict[str, Any]] | None = None,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_count: int = DEFAULT_READ_COUNT,
    compute_size: int = DEFAULT_CONTENTION_COMPUTE_SIZE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
    if any(str(command.get("type")).lower() != "read" for command in baseline):
        raise ValueError("baseline read-impact commands must all be read requests")
    contended = [*build_compute_prefix_commands(compute_size=compute_size), *[dict(command) for command in baseline]]
    if read_commands_only(contended) != baseline:
        raise ValueError("contention trace read portion does not match baseline")
    return baseline, contended


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


def write_read_impact_json(
    rows: list[dict[str, Any]],
    output_path: str | Path,
    *,
    baseline_trace_path: str | Path,
    contended_trace_path: str | Path,
) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "baseline_trace_path": str(normalize_path(baseline_trace_path)),
        "contended_trace_path": str(normalize_path(contended_trace_path)),
        "comparison": rows,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def write_read_impact_csv(rows: list[dict[str, Any]], output_path: str | Path) -> Path:
    path = normalize_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "type",
        "time",
        "start_lha",
        "size",
        "baseline_host_completion_time",
        "contended_host_completion_time",
        "completion_time_delta",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return path


def run_read_impact_comparison(
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    read_commands: list[dict[str, Any]] | None = None,
    read_count: int = DEFAULT_READ_COUNT,
    compute_size: int = DEFAULT_CONTENTION_COMPUTE_SIZE,
    engine_factory: Callable[[], Any] = Engine,
    simulate: Callable[..., SimulationResult] = run_engine_and_load_report,
) -> dict[str, Any]:
    output_root = normalize_path(output_root)
    baseline, contended = build_paired_read_impact_traces(
        read_commands=read_commands,
        pre_data_path=pre_data_path,
        read_count=read_count,
        compute_size=compute_size,
    )
    baseline_path, contended_path = write_read_impact_traces(output_root, baseline, contended)
    baseline_sim = simulate(baseline_path, engine_factory=engine_factory)
    contended_sim = simulate(contended_path, engine_factory=engine_factory)
    first_read_issue_time = int(baseline[0]["time"])
    validate_compute_prefix_overlap(contended_sim.report, first_read_issue_time=first_read_issue_time)
    rows = compare_read_completion_times(baseline_sim.report, contended_sim.report)

    results_dir = output_root / "results"
    json_path = write_read_impact_json(
        rows,
        results_dir / "read_impact_comparison.json",
        baseline_trace_path=baseline_path,
        contended_trace_path=contended_path,
    )
    csv_path = write_read_impact_csv(rows, results_dir / "read_impact_comparison.csv")
    return {
        "output_root": str(output_root),
        "baseline_trace_path": str(baseline_path),
        "contended_trace_path": str(contended_path),
        "results_json": str(json_path),
        "results_csv": str(csv_path),
        "comparison": rows,
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
        help="Number of default read requests in the read-impact experiment.",
    )
    parser.add_argument(
        "--compute-size",
        type=int,
        default=DEFAULT_CONTENTION_COMPUTE_SIZE,
        help="Size for each prepended compute request in the read-impact experiment.",
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
    print(f"[read-impact] compute-contention trace: {result['contended_trace_path']}")
    print(f"[read-impact] comparison JSON: {result['results_json']}")
    print(f"[read-impact] comparison CSV: {result['results_csv']}")


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
