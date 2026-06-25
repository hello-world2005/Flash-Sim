#!/usr/bin/env python3
"""Run the full GC pressure trace matrix and summarize maintenance metrics."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
VENV_PYTHON = WORKSPACE_ROOT / ".venv" / "bin" / "python"

PRESSURE_TRACES = [
    "gc_pressure_trace",
    "gc_pressure_trace_fast",
    "gc_pressure_trace_slow",
    "gc_pressure_trace_slow2",
    "gc_pressure_trace_wide",
    "gc_pressure_trace_5000000ns",
    "gc_pressure_trace_10000000ns",
    "gc_pressure_trace_15000000ns",
    "gc_pressure_trace_20ms",
]

SPECIAL_PRESSURE_TRACES = [
    "gc_pressure_low_invalid",
    "gc_pressure_concurrent_overwrite",
    "gc_pressure_post_flush_sustained",
    "gc_pressure_gc_reoverwrite",
]

AUXILIARY_TRACES = [
    "gc_stress_test",
    "gc_mini_test",
    "gc_test",
]


def _maybe_reexec_with_venv(disable_reexec: bool) -> None:
    if disable_reexec:
        return
    if os.environ.get("FLASH_SIM_MATRIX_VENV_REEXECED") == "1":
        return
    if not VENV_PYTHON.exists():
        return
    if Path(sys.executable) == VENV_PYTHON:
        return
    env = dict(os.environ)
    env["FLASH_SIM_MATRIX_VENV_REEXECED"] = "1"
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv], env)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run all Flash-Sim GC pressure trace variants.",
    )
    parser.add_argument(
        "--pressure-only",
        action="store_true",
        help="Run only gc_pressure_trace* variants, excluding auxiliary GC traces.",
    )
    parser.add_argument(
        "--trace",
        action="append",
        default=[],
        help="Run a specific trace stem or JSON filename. Can be repeated.",
    )
    parser.add_argument(
        "--output",
        default="report/gc_pressure_matrix_results.json",
        help="Path for the matrix summary JSON.",
    )
    parser.add_argument(
        "--log-dir",
        default="output",
        help="Directory for per-trace stdout/stderr logs.",
    )
    parser.add_argument(
        "--no-venv-reexec",
        action="store_true",
        help="Do not re-exec through the workspace .venv Python.",
    )
    parser.add_argument(
        "--trace-timeout",
        type=int,
        default=60,
        help="Maximum wall-clock seconds for one trace before it is marked failed.",
    )
    return parser.parse_args()


def _trace_stem(trace: str) -> str:
    return Path(trace).stem


def _trace_path(trace: str) -> Path:
    path = Path(trace)
    if path.suffix == ".json":
        return path if path.is_absolute() else REPO_ROOT / path
    return REPO_ROOT / "test_case" / f"{trace}.json"


def _selected_traces(args: argparse.Namespace) -> list[str]:
    if args.trace:
        return [_trace_stem(trace) for trace in args.trace]
    traces = [*PRESSURE_TRACES, *SPECIAL_PRESSURE_TRACES]
    if not args.pressure_only:
        traces.extend(AUXILIARY_TRACES)
    return traces


def _status_counts(requests: list[dict[str, Any]]) -> dict[str, int]:
    success = sum(1 for req in requests if req.get("status") == "SUCCESS")
    error = sum(1 for req in requests if req.get("status") == "ERROR")
    incomplete = sum(1 for req in requests if not req.get("status"))
    return {"success": success, "error": error, "incomplete": incomplete}


def _error_messages(requests: list[dict[str, Any]]) -> dict[str, int]:
    messages: dict[str, int] = {}
    for req in requests:
        if req.get("status") != "ERROR":
            continue
        message = req.get("error_message") or "<missing error_message>"
        messages[message] = messages.get(message, 0) + 1
    return messages


def _maintenance(report: dict[str, Any]) -> dict[str, Any]:
    return dict(report.get("meta", {}).get("maintenance", {}))


def _pending_cache_entries(engine: Any) -> int:
    cache = engine.device.hil.cache_manager.cache
    return len(cache.user_entries) + len(cache.static_entries)


def _waiting_write_count(engine: Any) -> int:
    waiting = engine.device.ftl.block_manager.waiting_writes
    return sum(len(items) for items in waiting.values())


def _summarize_report(
    *,
    trace: str,
    elapsed_s: float,
    report_path: Path,
    csv_path: Path | None,
    engine: Any,
) -> dict[str, Any]:
    with report_path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    requests = list(report.get("requests", []))
    counts = _status_counts(requests)
    error_messages = _error_messages(requests)
    maintenance = _maintenance(report)
    total = len(requests)
    write_count = sum(1 for req in requests if req.get("type") == "WRITE")

    return {
        "trace": trace,
        "status": "ok" if counts["incomplete"] == 0 else "incomplete",
        "elapsed_s": round(elapsed_s, 3),
        "total_requests": total,
        "write_requests": write_count,
        "read_or_other_requests": total - write_count,
        "success": counts["success"],
        "error": counts["error"],
        "error_messages": error_messages,
        "incomplete": counts["incomplete"],
        "final_time": report.get("meta", {}).get("final_time"),
        "report_path": str(report_path),
        "csv_path": str(csv_path) if csv_path is not None else None,
        "gc_count": maintenance.get("gc_count", 0),
        "static_wl_count": maintenance.get("static_wl_count", 0),
        "gc_relocated_pages": maintenance.get("gc_relocated_pages", 0),
        "gc_erased_blocks": maintenance.get("gc_erased_blocks", 0),
        "host_write_pages": maintenance.get("host_write_pages", 0),
        "physical_user_write_pages": maintenance.get("physical_user_write_pages", 0),
        "physical_gc_write_pages": maintenance.get("physical_gc_write_pages", 0),
        "write_amplification": maintenance.get("write_amplification", 0.0),
        "min_free_pool": maintenance.get("min_free_pool", 0),
        "max_wear_skew": maintenance.get("max_wear_skew", 0),
        "max_waiting_writes": maintenance.get("max_waiting_writes", 0),
        "current_waiting_writes": maintenance.get("current_waiting_writes", 0),
        "backpressure_wait_time": maintenance.get("backpressure_wait_time", 0),
        "residual_waiting_writes": _waiting_write_count(engine),
        "pending_cache_entries": _pending_cache_entries(engine),
    }


def _failure_result(
    *,
    trace: str,
    elapsed_s: float,
    exc: BaseException,
    report_path: Path,
    csv_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    return {
        "trace": trace,
        "status": "failed",
        "elapsed_s": round(elapsed_s, 3),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exc(),
        "report_path": str(report_path) if report_path.exists() else None,
        "csv_path": str(csv_path) if csv_path.exists() else None,
        "log_path": str(log_path),
    }


class TraceTimeoutError(TimeoutError):
    pass


@contextlib.contextmanager
def _trace_timeout(seconds: int):
    if seconds <= 0:
        yield
        return

    def raise_timeout(signum, frame):
        raise TraceTimeoutError(f"trace exceeded {seconds}s wall-clock timeout")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _run_trace(trace: str, log_dir: Path, *, timeout_s: int = 60) -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    from flash_sim.engine import Engine

    trace_path = _trace_path(trace)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{trace}.log"
    report_path = REPO_ROOT / "report" / f"{trace}_request_latency.json"
    csv_path = REPO_ROOT / "report" / f"{trace}_request_latency.csv"

    start = time.time()
    engine = None
    try:
        if not trace_path.exists():
            raise FileNotFoundError(f"Trace file not found: {trace_path}")
        with log_path.open("w", encoding="utf-8") as log_handle:
            with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(log_handle):
                with _trace_timeout(timeout_s):
                    engine = Engine()
                    engine.Start_simulation(str(trace_path))
        elapsed = time.time() - start
        actual_report = engine.last_request_latency_report_path or report_path
        actual_csv = engine.last_request_latency_csv_path or csv_path
        return {
            **_summarize_report(
                trace=trace,
                elapsed_s=elapsed,
                report_path=Path(actual_report),
                csv_path=Path(actual_csv) if actual_csv is not None else None,
                engine=engine,
            ),
            "log_path": str(log_path),
        }
    except BaseException as exc:
        elapsed = time.time() - start
        return _failure_result(
            trace=trace,
            elapsed_s=elapsed,
            exc=exc,
            report_path=report_path,
            csv_path=csv_path,
            log_path=log_path,
        )


def _validate_result(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if result.get("status") == "failed":
        return [result.get("error_message", "trace failed")]
    if result.get("incomplete", 0) > 0:
        issues.append(f"{result['incomplete']} requests missing terminal status")
    if result.get("error", 0) > 0:
        issues.append(f"{result['error']} requests completed with ERROR")
    if result.get("residual_waiting_writes", 0) > 0:
        issues.append(f"{result['residual_waiting_writes']} waiting writes remain")
    if result.get("pending_cache_entries", 0) > 0:
        issues.append(f"{result['pending_cache_entries']} cache entries remain")
    if not result.get("report_path"):
        issues.append("missing JSON report")
    if not result.get("csv_path"):
        issues.append("missing CSV report")

    clean_run = (
        result.get("status") == "ok"
        and result.get("incomplete", 0) == 0
        and result.get("error", 0) == 0
        and result.get("residual_waiting_writes", 0) == 0
        and result.get("pending_cache_entries", 0) == 0
        and bool(result.get("report_path"))
    )
    if clean_run:
        gc_count = int(result.get("gc_count", 0))
        static_wl_count = int(result.get("static_wl_count", 0))
        erased_blocks = int(result.get("gc_erased_blocks", 0))
        expected_erases = gc_count + static_wl_count
        if erased_blocks != expected_erases:
            issues.append(
                "maintenance erase mismatch: "
                f"started={expected_erases}, erased={erased_blocks}"
            )

        relocated_pages = int(result.get("gc_relocated_pages", 0))
        physical_gc_pages = int(result.get("physical_gc_write_pages", 0))
        if relocated_pages != physical_gc_pages:
            issues.append(
                "maintenance relocation mismatch: "
                f"planned={relocated_pages}, physical_gc_writes={physical_gc_pages}"
            )

        host_pages = int(result.get("host_write_pages", 0))
        physical_user_pages = int(result.get("physical_user_write_pages", 0))
        expected_wa = (
            (physical_user_pages + physical_gc_pages) / host_pages
            if host_pages > 0
            else 0.0
        )
        reported_wa = float(result.get("write_amplification", 0.0))
        if not math.isclose(reported_wa, expected_wa, rel_tol=1e-9, abs_tol=1e-9):
            issues.append(
                "write amplification mismatch: "
                f"reported={reported_wa}, expected={expected_wa}"
            )
    return issues


def _warnings_for_result(result: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if result.get("status") == "failed":
        return warnings
    write_count = result.get("write_requests", 0)
    wa = result.get("write_amplification", 0.0)
    if write_count > 0 and wa < 1.0:
        warnings.append(
            "write amplification below 1.0; controller write coalescing may be reducing media writes"
        )
    if result.get("error", 0) > 0 and not result.get("error_messages"):
        warnings.append("ERROR requests exist but no error_message was recorded")
    return warnings


def main() -> int:
    args = _parse_args()
    _maybe_reexec_with_venv(args.no_venv_reexec)

    traces = _selected_traces(args)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = REPO_ROOT / log_dir

    results: list[dict[str, Any]] = []
    print("=== Flash-Sim GC Pressure Matrix ===")
    print(f"Python: {sys.executable}")
    print(f"Traces: {len(traces)}")
    print()

    for index, trace in enumerate(traces, start=1):
        print(f"[{index}/{len(traces)}] Running {trace}...", end=" ", flush=True)
        result = _run_trace(trace, log_dir, timeout_s=args.trace_timeout)
        result["issues"] = _validate_result(result)
        result["warnings"] = _warnings_for_result(result)
        results.append(result)
        if result["status"] == "failed":
            print(f"FAILED ({result['error_type']}: {result['error_message']})")
            continue
        print(
            f"{result['elapsed_s']:.1f}s "
            f"{result['total_requests']} reqs "
            f"{result['success']} OK "
            f"{result['error']} ERR "
            f"GC={result['gc_count']} "
            f"WA={result['write_amplification']:.4f}"
        )

    summary = {
        "meta": {
            "python": sys.executable,
            "trace_count": len(traces),
            "pressure_traces": PRESSURE_TRACES,
            "special_pressure_traces": SPECIAL_PRESSURE_TRACES,
            "auxiliary_traces": [] if args.pressure_only else AUXILIARY_TRACES,
            "generated_at_unix": time.time(),
        },
        "results": results,
        "failed": [item["trace"] for item in results if item["status"] == "failed"],
        "with_issues": [item["trace"] for item in results if item.get("issues")],
        "with_warnings": [item["trace"] for item in results if item.get("warnings")],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print()
    print(f"Saved matrix summary to {output_path}")
    if summary["failed"] or summary["with_issues"]:
        print(f"Failed traces: {summary['failed']}")
        print(f"Traces with issues: {summary['with_issues']}")
        print(f"Traces with warnings: {summary['with_warnings']}")
        return 1
    if summary["with_warnings"]:
        print(f"Traces with warnings: {summary['with_warnings']}")
    print("All matrix traces completed without runner-level issues.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
