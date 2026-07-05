"""Run the run_test.md Flash-Sim/MQSim matrix and write test_result.md."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_validation as rv  # noqa: E402


TIMEOUT_S = 300
SECTOR_BYTES = 64
MIB = 1024 * 1024
RUN_TEST_TRACE_ROOT = SCRIPT_DIR / "traces/run_test"
RUN_TEST_TRACE_INPUTS = {
    "10k": {
        "flash": RUN_TEST_TRACE_ROOT / "exchange_disk0_page_10k_compact_flashsim.json",
        "mqsim": RUN_TEST_TRACE_ROOT / "exchange_disk0_page_10k_compact_mqsim.trace",
    },
    "30k": {
        "flash": RUN_TEST_TRACE_ROOT / "exchange_disk0_page_30k_compact_flashsim.json",
        "mqsim": RUN_TEST_TRACE_ROOT / "exchange_disk0_page_30k_compact_mqsim.trace",
    },
}
OUT_ROOT = SCRIPT_DIR / "out/run_test_matrix_latest"
RESULT_PATH = REPO_ROOT / "test_result.md"


def choose_python() -> str:
    candidates = [
        REPO_ROOT.parent / ".venv/bin/python",
        REPO_ROOT / ".venv/bin/python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def percentiles(values: list[float]) -> dict[str, float | None]:
    return {
        "p10": percentile(values, 10),
        "p50": percentile(values, 50),
        "p70": percentile(values, 70),
        "p90": percentile(values, 90),
    }


def ns_to_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 1_000_000.0


def ns_to_us(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 1_000.0


def ns_to_s(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 1_000_000_000.0


def fmt(value: Any, digits: int = 3) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def request_latency(req: dict[str, Any]) -> float:
    return float(req.get("host_total_latency", req.get("total_latency", 0)) or 0)


HOST_MEDIA_STAGES = (
    "amu_mapping_wait",
    "tsu_queue_wait",
    "phy_cmd_addr",
    "phy_data_in",
    "phy_array_exec",
    "phy_data_out",
)


def host_media_latency(req: dict[str, Any]) -> float:
    breakdown = req.get("breakdown", {}) or {}
    return sum(float(breakdown.get(stage, 0) or 0) for stage in HOST_MEDIA_STAGES)


def write_persistence_latency(req: dict[str, Any]) -> float | None:
    value = req.get("persistence_total_latency")
    if value is not None and float(value or 0) > 0:
        return float(value)
    # Older reports marked direct cache-forced/bypass writes as superseded even
    # though they completed through the host-visible NAND path. Treat those as
    # persisted at host completion when re-rendering existing reports.
    if req.get("type") == "WRITE" and host_media_latency(req) > 0:
        return request_latency(req)
    return None


def effective_write_persistence_status(req: dict[str, Any]) -> str:
    value = req.get("persistence_total_latency")
    if value is not None and float(value or 0) > 0:
        origin = str(req.get("persistence_origin") or "")
        if origin == "host_media_path":
            return "persisted_host_media"
        return "persisted_cache_flush"
    if req.get("type") == "WRITE" and host_media_latency(req) > 0:
        return "persisted_host_media"
    return "superseded_in_cache"


def trace_mb(requests: list[dict[str, Any]], req_type: str) -> float:
    total = 0
    for req in requests:
        if req.get("type") == req_type:
            total += int(req.get("size", 0)) * SECTOR_BYTES
    return total / MIB


def parse_flashsim_metrics(report_path: Path, runtime_s: float) -> dict[str, Any]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    requests = data.get("requests", [])
    reads = [r for r in requests if r.get("type") == "READ"]
    writes = [r for r in requests if r.get("type") == "WRITE"]
    maintenance = data.get("meta", {}).get("maintenance", {})
    host_write_pages = int(maintenance.get("host_write_pages", 0) or 0)
    physical_pages = int(maintenance.get("physical_user_write_pages", 0) or 0)
    physical_pages += int(maintenance.get("physical_gc_write_pages", 0) or 0)
    wa = maintenance.get("write_amplification")
    if wa is None and host_write_pages > 0:
        wa = physical_pages / host_write_pages
    write_host_latencies = [request_latency(r) for r in writes]
    write_cache_only_latencies = [
        request_latency(r)
        for r in writes
        if host_media_latency(r) == 0
    ]
    write_direct_host_latencies = [
        request_latency(r)
        for r in writes
        if host_media_latency(r) > 0
    ]
    write_persistence = [
        latency
        for req in writes
        if (latency := write_persistence_latency(req)) is not None
    ]
    persistence_status_counts = Counter(
        effective_write_persistence_status(r) for r in writes
    )
    return {
        "simulator": "flashsim",
        "program_runtime_s": runtime_s,
        "sim_completion_s": ns_to_s(data.get("meta", {}).get("final_time")),
        "read_requests": len(reads),
        "write_requests": len(writes),
        "request_count": len(requests),
        "success_count": sum(1 for r in requests if r.get("status") == "SUCCESS"),
        "error_count": sum(1 for r in requests if r.get("status") == "ERROR"),
        "read_mb": trace_mb(requests, "READ"),
        "write_mb": trace_mb(requests, "WRITE"),
        "gc_count": maintenance.get("gc_count"),
        "wa": wa,
        "read_host_ms": {k: ns_to_ms(v) for k, v in percentiles([request_latency(r) for r in reads]).items()},
        "write_host_ms": {k: ns_to_ms(v) for k, v in percentiles(write_host_latencies).items()},
        "write_cache_only_host_ms": {k: ns_to_ms(v) for k, v in percentiles(write_cache_only_latencies).items()},
        "write_direct_host_ms": {k: ns_to_ms(v) for k, v in percentiles(write_direct_host_latencies).items()},
        "read_host_us": {k: ns_to_us(v) for k, v in percentiles([request_latency(r) for r in reads]).items()},
        "write_host_us": {k: ns_to_us(v) for k, v in percentiles(write_host_latencies).items()},
        "write_cache_only_host_us": {k: ns_to_us(v) for k, v in percentiles(write_cache_only_latencies).items()},
        "write_direct_host_us": {k: ns_to_us(v) for k, v in percentiles(write_direct_host_latencies).items()},
        "write_cache_only_samples": len(write_cache_only_latencies),
        "write_direct_host_samples": len(write_direct_host_latencies),
        "write_persistence_ms": {k: ns_to_ms(v) for k, v in percentiles(write_persistence).items()},
        "write_persistence_us": {k: ns_to_us(v) for k, v in percentiles(write_persistence).items()},
        "write_persistence_samples": len(write_persistence),
        "write_persistence_status_counts": dict(sorted(persistence_status_counts.items())),
        "report": str(report_path),
    }


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_mqsim_metrics(report_path: Path, stdout_path: Path, runtime_s: float, logical: dict[str, Any]) -> dict[str, Any]:
    report = rv.parse_mqsim_report(report_path, stdout_path)
    totals = report.get("totals", {})
    ftl = report.get("ftl", {})
    request_count = int_or_zero(totals.get("request_count"))
    iops = float(totals.get("avg_iops", 0) or 0)
    if not iops:
        flows = report.get("flows", [])
        iops = sum(float(flow.get("IOPS", 0) or 0) for flow in flows)
    sim_completion_s = request_count / iops if iops > 0 else None
    programs = rv.mqsim_effective_program_pages(ftl)
    wa = programs / logical["write_requests"] if logical["write_requests"] else None
    return {
        "simulator": "mqsim",
        "program_runtime_s": runtime_s,
        "sim_completion_s": sim_completion_s,
        "read_requests": totals.get("read_count"),
        "write_requests": totals.get("write_count"),
        "request_count": totals.get("request_count"),
        "success_count": "",
        "error_count": "",
        "read_mb": logical["read_mb"],
        "write_mb": logical["write_mb"],
        "gc_count": ftl.get("Total_GC_Executions"),
        "wa": wa,
        "read_host_ms": {},
        "write_host_ms": {},
        "write_cache_only_host_ms": {},
        "write_direct_host_ms": {},
        "read_host_us": {},
        "write_host_us": {},
        "write_cache_only_host_us": {},
        "write_direct_host_us": {},
        "write_cache_only_samples": "",
        "write_direct_host_samples": "",
        "write_persistence_ms": {},
        "write_persistence_us": {},
        "write_persistence_samples": "",
        "write_persistence_status_counts": {},
        "report": str(report_path),
        "serviced_count": totals.get("stdout_serviced_request_count"),
        "generated_count": totals.get("stdout_generated_request_count"),
    }


def write_flashsim_config(path: Path, pre_pct: int, cache_mode: str) -> None:
    runtime = {
        "precondition_fill_ratio": pre_pct / 100.0,
        "precondition_mode": "capacity-fill",
        "precondition_seed": 42,
        "cache_bypass": cache_mode == "bypass",
        "data_cache_capacity": 65536 if cache_mode == "cache64" else 262144,
        "plane_allocation": "CWDP",
        "gc_low_watermark": 3,
        "stop_servicing_writes_threshold": 1,
        "gc_reserve_blocks": 1,
    }
    path.write_text(json.dumps({"runtime": runtime}, indent=2) + "\n", encoding="utf-8")


def run_flashsim(trace_path: Path, config_path: Path, profile: rv.Profile, case_dir: Path, python_bin: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    stdout_path = case_dir / "flashsim_stdout.log"
    report_path = REPO_ROOT / "report" / f"{trace_path.stem}_request_latency.json"
    command = [
        python_bin,
        "-m",
        "flash_sim.cli",
        "run-engine",
        str(trace_path.resolve()),
        "-c",
        str(config_path.resolve()),
        "--quiet",
        "--no-timeline",
        "--no-viz",
        "--fast-report",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(rv.flashsim_event_runtime_env(profile))
    start = time.perf_counter()
    try:
        with stdout_path.open("w", encoding="utf-8") as out:
            result = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                text=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT_S,
                env=env,
                check=False,
            )
        runtime_s = time.perf_counter() - start
    except subprocess.TimeoutExpired:
        runtime_s = time.perf_counter() - start
        return None, {
            "simulator": "flashsim",
            "status": "timeout",
            "program_runtime_s": runtime_s,
            "stdout": str(stdout_path),
        }
    if result.returncode != 0 or not report_path.exists():
        return None, {
            "simulator": "flashsim",
            "status": f"exit {result.returncode}",
            "program_runtime_s": runtime_s,
            "stdout": str(stdout_path),
        }
    return parse_flashsim_metrics(report_path, runtime_s), None


def run_mqsim(paths: dict[str, Path], mqsim_bin: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    stdout_path = paths["workload"].with_name("mqsim_stdout.log")
    report_path = paths["workload"].with_name(f"{paths['workload'].stem}_scenario_1.xml")
    command = [
        str(mqsim_bin),
        "-i",
        str(paths["ssd_config"].resolve()),
        "-w",
        str(paths["workload"].resolve()),
    ]
    start = time.perf_counter()
    try:
        with stdout_path.open("w", encoding="utf-8") as out:
            result = subprocess.run(
                command,
                cwd=str(rv.MQSIM_ROOT),
                text=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT_S,
                check=False,
            )
        runtime_s = time.perf_counter() - start
    except subprocess.TimeoutExpired:
        runtime_s = time.perf_counter() - start
        return None, {
            "simulator": "mqsim",
            "status": "timeout",
            "program_runtime_s": runtime_s,
            "stdout": str(stdout_path),
        }
    if result.returncode != 0 or not report_path.exists():
        return None, {
            "simulator": "mqsim",
            "status": f"exit {result.returncode}",
            "program_runtime_s": runtime_s,
            "stdout": str(stdout_path),
        }
    return {"report_path": report_path, "stdout_path": stdout_path, "runtime_s": runtime_s}, None


def make_profiles() -> dict[str, rv.Profile]:
    base = rv.PROFILES["flashsim-event-small"]
    return {
        "small64": replace(base, name="run-test-small64", block_no_per_plane=64),
        "modern256": replace(base, name="run-test-modern256", block_no_per_plane=256),
    }


def case_plan() -> list[dict[str, Any]]:
    plans = []
    for trace_label, max_requests, preconditions in (
        ("10k", 10000, (25, 50, 75)),
        ("30k", 30000, (25, 50)),
    ):
        for geometry in ("small64", "modern256"):
            for pre_pct in preconditions:
                for cache_mode in ("bypass", "cache64"):
                    plans.append(
                        {
                            "trace": trace_label,
                            "max_requests": max_requests,
                            "geometry": geometry,
                            "pre_pct": pre_pct,
                            "cache_mode": cache_mode,
                        }
                    )
    return plans


def logical_stats(paths: dict[str, Path]) -> dict[str, Any]:
    data = json.loads(paths["flash_trace"].read_text(encoding="utf-8"))
    reads = [item for item in data if item["type"] == "read"]
    writes = [item for item in data if item["type"] == "write"]
    return {
        "read_requests": len(reads),
        "write_requests": len(writes),
        "request_count": len(data),
        "read_mb": sum(int(item["size"]) * SECTOR_BYTES for item in reads) / MIB,
        "write_mb": sum(int(item["size"]) * SECTOR_BYTES for item in writes) / MIB,
    }


def render_report(results: list[dict[str, Any]], failures: list[dict[str, Any]], run_id: str) -> str:
    lines = [
        "# run_test.md 测试结果",
        "",
        f"- 运行时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 输出目录: `{OUT_ROOT / run_id}`",
        f"- Flash-Sim timeout: {TIMEOUT_S}s",
        "- Trace: `validation/mqsim_flash/traces/run_test` 中固定的 Exchange disk0 compact-normalized traces；Flash-Sim 使用 64B/LHA sector。",
        "- small geometry: `FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE=64`；modern geometry: `256`。",
        "- cache 模式: `bypass` 表示 `cache_bypass=true`；`cache64` 表示 cache enabled 且 `data_cache_capacity=65536`。",
        "- MQSim 若未生成有效 XML 或 exit code 非 0，则不在成功结果表中汇报，只在失败/跳过表中记录。",
        "",
        "## 成功运行汇总",
        "",
        "| 几何 | trace | pre% | cache | simulator | 程序时间(s) | 模拟完成时间(s) | 读请求 | 写请求 | 读数据(MB) | 写数据(MB) | GC | WA | 完成/错误 |",
        "|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        complete = ""
        if row["simulator"] == "flashsim":
            complete = f"{row.get('success_count', '')}/{row.get('request_count', '')}, err={row.get('error_count', '')}"
        else:
            serviced = row.get("serviced_count")
            generated = row.get("generated_count")
            complete = f"serviced={serviced}/{generated}" if serviced or generated else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["geometry"]),
                    str(row["trace"]),
                    str(row["pre_pct"]),
                    str(row["cache_mode"]),
                    str(row["simulator"]),
                    fmt(row.get("program_runtime_s"), 3),
                    fmt(row.get("sim_completion_s"), 6),
                    fmt(row.get("read_requests"), 0),
                    fmt(row.get("write_requests"), 0),
                    fmt(row.get("read_mb"), 2),
                    fmt(row.get("write_mb"), 2),
                    fmt(row.get("gc_count"), 0),
                    fmt(row.get("wa"), 4),
                    complete,
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Flash-Sim Host 延迟 Percentile (us)",
            "",
            "这里的 write percentile 是全部写请求的 host-visible 延迟；cache64 下若 cache 满或 flush 被 GC 阻塞，部分写会转入 direct/media host path，因此需要结合后面的路径拆分表一起看。",
            "",
            "| 几何 | trace | pre% | cache | read P10 | read P50 | read P70 | read P90 | write P10 | write P50 | write P70 | write P90 |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        read = row.get("read_host_us", {})
        write = row.get("write_host_us", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["geometry"]),
                    str(row["trace"]),
                    str(row["pre_pct"]),
                    str(row["cache_mode"]),
                    fmt(read.get("p10"), 3),
                    fmt(read.get("p50"), 3),
                    fmt(read.get("p70"), 3),
                    fmt(read.get("p90"), 3),
                    fmt(write.get("p10"), 3),
                    fmt(write.get("p50"), 3),
                    fmt(write.get("p70"), 3),
                    fmt(write.get("p90"), 3),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Flash-Sim 写 Host 路径拆分",
            "",
            "`cache-only` 表示 host 完成前没有进入 AMU/TSU/PHY；`direct/media` 表示 bypass 或 cache forced-bypass，host 完成需要等待 NAND 路径。状态计数为有效持久化状态，兼容旧 raw report 的标签问题。",
            "",
            "| 几何 | trace | pre% | cache | cache-only samples | direct/media samples | cache-only P50(us) | cache-only P90(us) | direct/media P50(us) | direct/media P90(us) | persistence 状态计数 |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        cache_pct = row.get("write_cache_only_host_us", {})
        direct_pct = row.get("write_direct_host_us", {})
        status_counts = row.get("write_persistence_status_counts", {}) or {}
        status_text = ", ".join(
            f"{key}={value}" for key, value in status_counts.items()
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["geometry"]),
                    str(row["trace"]),
                    str(row["pre_pct"]),
                    str(row["cache_mode"]),
                    fmt(row.get("write_cache_only_samples"), 0),
                    fmt(row.get("write_direct_host_samples"), 0),
                    fmt(cache_pct.get("p50"), 3),
                    fmt(cache_pct.get("p90"), 3),
                    fmt(direct_pct.get("p50"), 3),
                    fmt(direct_pct.get("p90"), 3),
                    status_text,
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Flash-Sim 写请求持久化延迟 Percentile (us)",
            "",
            "cache-bypass/direct-media 写的 host completion 已是 NAND 完成边界；cache64 中后台 flush 成功的写使用 persistence latency；被 cache 覆盖、没有单独落盘的 superseded 写不计入 samples。",
            "",
            "| 几何 | trace | pre% | cache | samples | write persistence P10 | P50 | P70 | P90 |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        pct = row.get("write_persistence_us", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["geometry"]),
                    str(row["trace"]),
                    str(row["pre_pct"]),
                    str(row["cache_mode"]),
                    fmt(row.get("write_persistence_samples"), 0),
                    fmt(pct.get("p10"), 3),
                    fmt(pct.get("p50"), 3),
                    fmt(pct.get("p70"), 3),
                    fmt(pct.get("p90"), 3),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## MQSim 可用延迟字段",
            "",
            "MQSim 当前 XML 没有请求级 P10/P50/P70/P90，因此本报告不伪造 MQSim percentile；成功 MQSim 行只汇报程序时间、估算模拟完成时间、请求数、数据量、GC 和 WA。",
            "",
        ]
    )

    if failures:
        lines.extend(
            [
                "## 失败或跳过",
                "",
                "| 几何 | trace | pre% | cache | simulator | 状态 | 程序时间(s) | log |",
                "|---|---|---:|---|---|---|---:|---|",
            ]
        )
        for item in failures:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(item.get("geometry", "")),
                        str(item.get("trace", "")),
                        str(item.get("pre_pct", "")),
                        str(item.get("cache_mode", "")),
                        str(item.get("simulator", "")),
                        str(item.get("status", "")),
                        fmt(item.get("program_runtime_s"), 3),
                        f"`{item.get('stdout', '')}`" if item.get("stdout") else "",
                    ]
                )
                + " |"
            )

    lines.extend(
        [
            "",
            "## 原始结果",
            "",
            f"- JSON summary: `{OUT_ROOT / run_id / 'summary.json'}`",
            "- Flash-Sim request latency reports are in `report/*_request_latency.json` and referenced from the JSON summary.",
        ]
    )
    return "\n".join(lines) + "\n"


def rerender_existing(run_id: str) -> int:
    run_root = OUT_ROOT / run_id
    summary_path = run_root / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    refreshed_results: list[dict[str, Any]] = []
    for row in summary.get("results", []):
        if row.get("simulator") != "flashsim":
            refreshed_results.append(row)
            continue
        report_path = Path(row.get("report", ""))
        if not report_path.exists():
            refreshed_results.append(row)
            continue
        refreshed = parse_flashsim_metrics(
            report_path,
            float(row.get("program_runtime_s", 0) or 0),
        )
        for key in ("geometry", "trace", "pre_pct", "cache_mode"):
            refreshed[key] = row.get(key)
        refreshed_results.append(refreshed)

    refreshed_summary = {
        "run_id": run_id,
        "results": refreshed_results,
        "failures": summary.get("failures", []),
    }
    summary_path.write_text(json.dumps(refreshed_summary, indent=2) + "\n", encoding="utf-8")
    RESULT_PATH.write_text(
        render_report(refreshed_results, refreshed_summary["failures"], run_id),
        encoding="utf-8",
    )
    print(f"[done] re-rendered {RESULT_PATH}", flush=True)
    print(f"[done] summary {summary_path}", flush=True)
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--rerender":
        return rerender_existing(sys.argv[2])
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = OUT_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    python_bin = choose_python()
    mqsim_bin, notes = rv.ensure_mqsim_binary(rv.MQSIM_ROOT / "MQSim", skip_build=True, timeout=TIMEOUT_S)
    if notes:
        for note in notes:
            print(f"[setup] {note}", flush=True)
    profiles = make_profiles()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    plans = case_plan()
    total_steps = len(plans)
    for index, plan in enumerate(plans, start=1):
        profile = profiles[plan["geometry"]]
        cache_profile = profile
        if plan["cache_mode"] == "cache64":
            cache_profile = replace(profile, data_cache_capacity=65536)
        case_name = (
            f"exchange_disk0_page_{plan['trace']}_{plan['geometry']}_"
            f"pre{plan['pre_pct']}_{plan['cache_mode']}_{run_id}"
        )
        case_dir = run_root / plan["geometry"] / plan["trace"] / f"pre{plan['pre_pct']}" / plan["cache_mode"]
        print(
            f"[{index}/{total_steps}] {plan['geometry']} {plan['trace']} "
            f"pre{plan['pre_pct']} {plan['cache_mode']}: prepare",
            flush=True,
        )
        trace_inputs = RUN_TEST_TRACE_INPUTS[plan["trace"]]
        case = rv.build_external_trace_case(
            cache_profile,
            trace_inputs["flash"],
            trace_inputs["mqsim"],
            name=case_name,
            max_requests=None,
            address_mode="raw",
            precondition_mode="none",
            mqsim_preconditioning=True,
            mqsim_initial_occupancy_percentage=plan["pre_pct"],
        )
        case = replace(
            case,
            mqsim_cache_mode="TURNED_OFF" if plan["cache_mode"] == "bypass" else "WRITE_CACHE",
            flashsim_cache_bypass=plan["cache_mode"] == "bypass",
            flashsim_plane_allocation="CWDP",
        )
        paths = rv.write_case_inputs(case, cache_profile, case_dir)
        config_path = case_dir / "flashsim_config.json"
        write_flashsim_config(config_path, plan["pre_pct"], plan["cache_mode"])
        logical = logical_stats(paths)

        print(f"[{index}/{total_steps}] flashsim start", flush=True)
        flash_metrics, flash_failure = run_flashsim(paths["flash_trace"], config_path, profile, case_dir, python_bin)
        if flash_metrics is not None:
            flash_metrics.update(plan)
            results.append(flash_metrics)
            print(
                f"[{index}/{total_steps}] flashsim ok "
                f"runtime={flash_metrics['program_runtime_s']:.3f}s "
                f"success={flash_metrics['success_count']}/{flash_metrics['request_count']} "
                f"gc={flash_metrics['gc_count']}",
                flush=True,
            )
        else:
            flash_failure.update(plan)
            failures.append(flash_failure)
            print(f"[{index}/{total_steps}] flashsim {flash_failure['status']}", flush=True)

        if mqsim_bin is None:
            failures.append(
                {
                    **plan,
                    "simulator": "mqsim",
                    "status": "missing binary",
                    "program_runtime_s": None,
                    "stdout": "",
                }
            )
            print(f"[{index}/{total_steps}] mqsim skipped: missing binary", flush=True)
            continue

        print(f"[{index}/{total_steps}] mqsim start", flush=True)
        mq_run, mq_failure = run_mqsim(paths, mqsim_bin)
        if mq_run is None:
            mq_failure.update(plan)
            failures.append(mq_failure)
            print(f"[{index}/{total_steps}] mqsim {mq_failure['status']}", flush=True)
            continue
        mq_metrics = parse_mqsim_metrics(
            mq_run["report_path"],
            mq_run["stdout_path"],
            mq_run["runtime_s"],
            logical,
        )
        mq_metrics.update(plan)
        results.append(mq_metrics)
        print(
            f"[{index}/{total_steps}] mqsim ok runtime={mq_metrics['program_runtime_s']:.3f}s "
            f"gc={mq_metrics['gc_count']}",
            flush=True,
        )

        summary = {"run_id": run_id, "results": results, "failures": failures}
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        RESULT_PATH.write_text(render_report(results, failures, run_id), encoding="utf-8")

    summary = {"run_id": run_id, "results": results, "failures": failures}
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    RESULT_PATH.write_text(render_report(results, failures, run_id), encoding="utf-8")
    latest_link = OUT_ROOT / "latest"
    try:
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_root, target_is_directory=True)
    except OSError:
        pass
    print(f"[done] wrote {RESULT_PATH}", flush=True)
    print(f"[done] summary {run_root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
