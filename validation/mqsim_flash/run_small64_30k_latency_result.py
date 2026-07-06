"""Run requested small64 preconditioning latency reports."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_test_matrix_latest as rtm  # noqa: E402
import run_validation as rv  # noqa: E402


OUT_ROOT = SCRIPT_DIR / "out/small64_30k_latency_result"
RESULT_PATH = REPO_ROOT / "result_small64_30k.md"
TIMEOUT_S = 300


def case_plan(
    trace_label: str,
    cache_modes: tuple[str, ...],
    preconditions: tuple[int, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "trace": trace_label,
            "geometry": "small64",
            "pre_pct": pre_pct,
            "cache_mode": cache_mode,
        }
        for pre_pct in preconditions
        for cache_mode in cache_modes
    ]


def trace_inputs_and_address_mode(trace_label: str) -> tuple[dict[str, Path], str]:
    if trace_label in rtm.RUN_TEST_TRACE_INPUTS:
        return rtm.RUN_TEST_TRACE_INPUTS[trace_label], "raw"
    if trace_label == "50k":
        public_root = SCRIPT_DIR / "public_traces/exchange"
        return (
            {
                "flash": public_root / "exchange_disk0_page_50000_flashsim.json",
                "mqsim": public_root / "exchange_disk0_page_50000_mqsim.trace",
            },
            "compact",
        )
    raise ValueError(f"unsupported trace label: {trace_label}")


def write_row(values: list[Any]) -> str:
    return "| " + " | ".join(str(value) for value in values) + " |"


def status_text(row: dict[str, Any]) -> str:
    if row["simulator"] == "flashsim":
        text = (
            f"{row.get('success_count', '')}/{row.get('request_count', '')}, "
            f"err={row.get('error_count', '')}"
        )
        if row.get("unknown_status_count"):
            text += f", other={row.get('unknown_status_count')}"
        return text
    serviced = row.get("serviced_count")
    generated = row.get("generated_count")
    if serviced or generated:
        return f"serviced={serviced}/{generated}"
    return ""


def render_report(
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    run_id: str,
    out_root: Path,
    trace_label: str,
    cache_modes: tuple[str, ...],
    preconditions: tuple[int, ...],
) -> str:
    run_root = out_root / run_id
    cache_text = "/".join(cache_modes)
    precondition_text = "/".join(f"{pre_pct}%" for pre_pct in preconditions)
    lines = [
        f"# small64 {trace_label} precondition latency result",
        "",
        f"- 运行时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 输出目录: `{run_root}`",
        f"- timeout: {TIMEOUT_S}s per simulator invocation",
        f"- 范围: `{trace_label}` trace, `small64` geometry, precondition `{precondition_text}`, cache `{cache_text}`。",
        "- MQSim README 口径: `Enabled_Preconditioning=true` 启用内置 preconditioning；`Initial_Occupancy_Percentage` 是 preconditioning 期间填充的 logical pages 百分比。",
        "- 本次 MQSim 使用 built-in preconditioning 和对应 `Initial_Occupancy_Percentage`；Flash-Sim 使用 runtime `precondition_fill_ratio` 做同百分比 capacity-fill。",
        "- cache64 对齐口径: Flash-Sim 使用 `data_cache_capacity=65536`；MQSim 使用 `Data_Cache_Capacity=65536` 且 workload flow `Device_Level_Data_Caching_Mode=WRITE_CACHE`。容量/模式对齐，但两个模拟器的 cache/flush 语义不保证完全相同。",
        "- Flash-Sim GC 对齐口径: 本脚本写入 `gc_exec_threshold=0.05`、`gc_victim_policy=d-choices`、`gc_d_choices=6`。`d=6` 来自 MQSim page-level RGA 对 `small64` 的 `log2(block_no_per_plane)`。",
        "- GC 次数对比要先看 MQSim `serviced/generated`；如果 MQSim 没有 drain 完整 trace，它的 GC 只覆盖已 serviced 的前缀负载，不能直接和 Flash-Sim 全 trace GC 相除比较。",
        "- MQSim XML 没有请求级 P10/P50/P70/P90；因此 MQSim 只汇报 XML 中的 min/avg/max 聚合延迟。",
        "",
        "## 成功运行汇总",
        "",
        "| pre% | cache | simulator | 程序时间(s) | 模拟完成时间(s) | 读请求 | 写请求 | 读数据(MB) | 写数据(MB) | GC | WA | 完成/错误 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in results:
        lines.append(
            write_row(
                [
                    row["pre_pct"],
                    row["cache_mode"],
                    row["simulator"],
                    rtm.fmt(row.get("program_runtime_s"), 3),
                    rtm.fmt(row.get("sim_completion_s"), 6),
                    rtm.fmt(row.get("read_requests"), 0),
                    rtm.fmt(row.get("write_requests"), 0),
                    rtm.fmt(row.get("read_mb"), 2),
                    rtm.fmt(row.get("write_mb"), 2),
                    rtm.fmt(row.get("gc_count"), 0),
                    rtm.fmt(row.get("wa"), 4),
                    status_text(row),
                ]
            )
        )

    lines.extend(
        [
            "",
            "`other` 表示 Flash-Sim raw request report 中状态既不是 `SUCCESS` 也不是 `ERROR` 的请求，通常需要结合 `summary.json` 的 `status_counts` 继续检查。",
        ]
    )

    lines.extend(
        [
            "",
            "## Flash-Sim Host 延迟 (us)",
            "",
            "| pre% | cache | read avg | read P10 | read P50 | read P70 | read P90 | write avg | write P10 | write P50 | write P70 | write P90 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        read = row.get("read_host_us", {}) or {}
        write = row.get("write_host_us", {}) or {}
        lines.append(
            write_row(
                [
                    row["pre_pct"],
                    row["cache_mode"],
                    rtm.fmt(read.get("avg"), 3),
                    rtm.fmt(read.get("p10"), 3),
                    rtm.fmt(read.get("p50"), 3),
                    rtm.fmt(read.get("p70"), 3),
                    rtm.fmt(read.get("p90"), 3),
                    rtm.fmt(write.get("avg"), 3),
                    rtm.fmt(write.get("p10"), 3),
                    rtm.fmt(write.get("p50"), 3),
                    rtm.fmt(write.get("p70"), 3),
                    rtm.fmt(write.get("p90"), 3),
                ]
            )
        )

    lines.extend(
        [
            "",
            "## Flash-Sim 写 Host 路径拆分 (us)",
            "",
            "| pre% | cache | cache-only samples | direct/media samples | cache-only avg | cache-only P50 | cache-only P90 | direct/media avg | direct/media P50 | direct/media P90 | persistence 状态计数 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        cache_pct = row.get("write_cache_only_host_us", {}) or {}
        direct_pct = row.get("write_direct_host_us", {}) or {}
        status_counts = row.get("write_persistence_status_counts", {}) or {}
        status_counts_text = ", ".join(
            f"{key}={value}" for key, value in status_counts.items()
        )
        lines.append(
            write_row(
                [
                    row["pre_pct"],
                    row["cache_mode"],
                    rtm.fmt(row.get("write_cache_only_samples"), 0),
                    rtm.fmt(row.get("write_direct_host_samples"), 0),
                    rtm.fmt(cache_pct.get("avg"), 3),
                    rtm.fmt(cache_pct.get("p50"), 3),
                    rtm.fmt(cache_pct.get("p90"), 3),
                    rtm.fmt(direct_pct.get("avg"), 3),
                    rtm.fmt(direct_pct.get("p50"), 3),
                    rtm.fmt(direct_pct.get("p90"), 3),
                    status_counts_text,
                ]
            )
        )

    lines.extend(
        [
            "",
            "## Flash-Sim 写持久化延迟 (us)",
            "",
            "| pre% | cache | samples | avg | P10 | P50 | P70 | P90 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in results:
        if row["simulator"] != "flashsim":
            continue
        pct = row.get("write_persistence_us", {}) or {}
        lines.append(
            write_row(
                [
                    row["pre_pct"],
                    row["cache_mode"],
                    rtm.fmt(row.get("write_persistence_samples"), 0),
                    rtm.fmt(pct.get("avg"), 3),
                    rtm.fmt(pct.get("p10"), 3),
                    rtm.fmt(pct.get("p50"), 3),
                    rtm.fmt(pct.get("p70"), 3),
                    rtm.fmt(pct.get("p90"), 3),
                ]
            )
        )

    lines.extend(
        [
            "",
            "## MQSim XML 延迟 (us)",
            "",
        ]
    )
    mqsim_rows = [row for row in results if row["simulator"] == "mqsim"]
    if mqsim_rows:
        lines.extend(
            [
                "| pre% | cache | Device min | Device avg | Device max | E2E min | E2E avg | E2E max | Read txn avg | Write txn avg |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in mqsim_rows:
            device = row.get("mqsim_device_response_us", {}) or {}
            e2e = row.get("mqsim_end_to_end_us", {}) or {}
            txn = row.get("mqsim_transaction_us", {}) or {}
            lines.append(
                write_row(
                    [
                        row["pre_pct"],
                        row["cache_mode"],
                        rtm.fmt(device.get("min"), 3),
                        rtm.fmt(device.get("avg"), 3),
                        rtm.fmt(device.get("max"), 3),
                        rtm.fmt(e2e.get("min"), 3),
                        rtm.fmt(e2e.get("avg"), 3),
                        rtm.fmt(e2e.get("max"), 3),
                        rtm.fmt(txn.get("read_turnaround_avg"), 3),
                        rtm.fmt(txn.get("write_turnaround_avg"), 3),
                    ]
                )
            )
    else:
        lines.append("本次没有成功生成可解析 XML 的 MQSim case。")

    if failures:
        lines.extend(
            [
                "",
                "## 失败或跳过",
                "",
                "| pre% | cache | simulator | 状态 | 程序时间(s) | log |",
                "|---:|---|---|---|---:|---|",
            ]
        )
        for item in failures:
            log = f"`{item.get('stdout', '')}`" if item.get("stdout") else ""
            lines.append(
                write_row(
                    [
                        item.get("pre_pct", ""),
                        item.get("cache_mode", ""),
                        item.get("simulator", ""),
                        item.get("status", ""),
                        rtm.fmt(item.get("program_runtime_s"), 3),
                        log,
                    ]
                )
            )

    lines.extend(
        [
            "",
            "## 原始结果",
            "",
            f"- JSON summary: `{run_root / 'summary.json'}`",
            "- Flash-Sim request latency reports are in `report/*_request_latency.json` and referenced from the JSON summary.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    run_id: str,
    run_root: Path,
    result_path: Path,
    out_root: Path,
    trace_label: str,
    cache_modes: tuple[str, ...],
    preconditions: tuple[int, ...],
) -> None:
    summary = {
        "run_id": run_id,
        "trace_label": trace_label,
        "cache_modes": list(cache_modes),
        "preconditions": list(preconditions),
        "results": results,
        "failures": failures,
    }
    (run_root / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    result_path.write_text(
        render_report(
            results,
            failures,
            run_id,
            out_root,
            trace_label,
            cache_modes,
            preconditions,
        ),
        encoding="utf-8",
    )


def rerender_existing(run_id: str, result_path: Path, out_root: Path) -> int:
    run_root = out_root / run_id
    summary_path = run_root / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trace_label = str(summary.get("trace_label") or "30k")
    cache_modes = tuple(summary.get("cache_modes") or ("bypass", "cache64"))
    preconditions = tuple(summary.get("preconditions") or (25, 50, 75))
    refreshed_results: list[dict[str, Any]] = []
    for row in summary.get("results", []):
        if row.get("simulator") != "flashsim":
            refreshed_results.append(row)
            continue
        report_path = Path(row.get("report", ""))
        if not report_path.exists():
            refreshed_results.append(row)
            continue
        refreshed = rtm.parse_flashsim_metrics(
            report_path,
            float(row.get("program_runtime_s", 0) or 0),
        )
        for key in ("trace", "geometry", "pre_pct", "cache_mode"):
            refreshed[key] = row.get(key)
        refreshed_results.append(refreshed)

    write_outputs(
        refreshed_results,
        summary.get("failures", []),
        run_id,
        run_root,
        result_path,
        out_root,
        trace_label,
        cache_modes,
        preconditions,
    )
    print(f"[done] re-rendered {result_path}", flush=True)
    print(f"[done] summary {summary_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-path", type=Path, default=RESULT_PATH)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--rerender", default=None)
    parser.add_argument("--trace-label", default="30k", choices=["10k", "30k", "50k"])
    parser.add_argument("--precondition", type=int, action="append", default=None)
    parser.add_argument(
        "--cache-mode",
        action="append",
        choices=["bypass", "cache64"],
        default=None,
    )
    args = parser.parse_args(argv)

    if args.rerender:
        return rerender_existing(args.rerender, args.result_path, args.out_root)

    cache_modes = tuple(args.cache_mode or ("bypass", "cache64"))
    preconditions = tuple(args.precondition or (25, 50, 75))
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = args.out_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    trace_inputs, address_mode = trace_inputs_and_address_mode(args.trace_label)

    python_bin = rtm.choose_python()
    mqsim_bin, notes = rv.ensure_mqsim_binary(
        rv.MQSIM_ROOT / "MQSim",
        skip_build=True,
        timeout=TIMEOUT_S,
    )
    for note in notes:
        print(f"[setup] {note}", flush=True)

    profile = replace(
        rv.PROFILES["flashsim-event-small"],
        name="run-test-small64",
        block_no_per_plane=64,
    )
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    plans = case_plan(args.trace_label, cache_modes, preconditions)

    for index, plan in enumerate(plans, start=1):
        cache_profile = (
            replace(profile, data_cache_capacity=65536)
            if plan["cache_mode"] == "cache64"
            else profile
        )
        case_name = (
            f"exchange_disk0_page_{args.trace_label}_small64_pre{plan['pre_pct']}_"
            f"{plan['cache_mode']}_{run_id}"
        )
        case_dir = (
            run_root
            / "small64"
            / args.trace_label
            / f"pre{plan['pre_pct']}"
            / plan["cache_mode"]
        )
        print(
            f"[{index}/{len(plans)}] small64 {args.trace_label} pre{plan['pre_pct']} "
            f"{plan['cache_mode']}: prepare",
            flush=True,
        )

        case = rv.build_external_trace_case(
            cache_profile,
            trace_inputs["flash"],
            trace_inputs["mqsim"],
            name=case_name,
            max_requests=None,
            address_mode=address_mode,
            precondition_mode="none",
            mqsim_preconditioning=True,
            mqsim_initial_occupancy_percentage=plan["pre_pct"],
        )
        case = replace(
            case,
            mqsim_cache_mode=(
                "TURNED_OFF" if plan["cache_mode"] == "bypass" else "WRITE_CACHE"
            ),
            flashsim_cache_bypass=plan["cache_mode"] == "bypass",
            flashsim_plane_allocation="CWDP",
        )
        paths = rv.write_case_inputs(case, cache_profile, case_dir)
        config_path = case_dir / "flashsim_config.json"
        rtm.write_flashsim_config(config_path, plan["pre_pct"], plan["cache_mode"])
        logical = rtm.logical_stats(paths)

        print(f"[{index}/{len(plans)}] flashsim start", flush=True)
        flash_metrics, flash_failure = rtm.run_flashsim(
            paths["flash_trace"],
            config_path,
            profile,
            case_dir,
            python_bin,
        )
        if flash_metrics is not None:
            flash_metrics.update(plan)
            results.append(flash_metrics)
            print(
                f"[{index}/{len(plans)}] flashsim ok "
                f"runtime={flash_metrics['program_runtime_s']:.3f}s "
                f"success={flash_metrics['success_count']}/{flash_metrics['request_count']} "
                f"gc={flash_metrics['gc_count']}",
                flush=True,
            )
        else:
            flash_failure.update(plan)
            failures.append(flash_failure)
            print(
                f"[{index}/{len(plans)}] flashsim {flash_failure['status']}",
                flush=True,
            )

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
            print(f"[{index}/{len(plans)}] mqsim skipped: missing binary", flush=True)
            write_outputs(
                results,
                failures,
                run_id,
                run_root,
                args.result_path,
                args.out_root,
                args.trace_label,
                cache_modes,
                preconditions,
            )
            continue

        print(f"[{index}/{len(plans)}] mqsim start", flush=True)
        mq_run, mq_failure = rtm.run_mqsim(paths, mqsim_bin)
        if mq_run is None:
            mq_failure.update(plan)
            failures.append(mq_failure)
            print(f"[{index}/{len(plans)}] mqsim {mq_failure['status']}", flush=True)
        else:
            mq_metrics = rtm.parse_mqsim_metrics(
                mq_run["report_path"],
                mq_run["stdout_path"],
                mq_run["runtime_s"],
                logical,
            )
            mq_metrics.update(plan)
            results.append(mq_metrics)
            print(
                f"[{index}/{len(plans)}] mqsim ok "
                f"runtime={mq_metrics['program_runtime_s']:.3f}s "
                f"gc={mq_metrics['gc_count']}",
                flush=True,
            )

        write_outputs(
            results,
            failures,
            run_id,
            run_root,
            args.result_path,
            args.out_root,
            args.trace_label,
            cache_modes,
            preconditions,
        )

    latest_link = args.out_root / "latest"
    try:
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_root, target_is_directory=True)
    except OSError:
        pass

    print(f"[done] wrote {args.result_path}", flush=True)
    print(f"[done] summary {run_root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
