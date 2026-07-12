#!/usr/bin/env python3
"""Run the mixed-size read-only trace with equivalent valid-page warmup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import run_validation as rv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    profile = rv.PROFILES["flashsim-event-small"]
    source = json.loads(
        (args.trace_dir / "complex_read_1k_64b_flashsim.json").read_text(encoding="utf-8")
    )
    operations = []
    for index, record in enumerate(source):
        operations.append(
            {
                "type": "read",
                "time": int(record["time"]),
                "page_id": int(record["start_lha"]) // rv.FLASHSIM_SECTORS_PER_PAGE,
                "flashsim_start_lha": int(record["start_lha"]),
                "flashsim_size": int(record["size"]),
                "mqsim_start_sector": int(record["start_lha"]),
                "mqsim_size_sectors": int(record["size"]),
                "phase": "main",
                "source_index": index,
            }
        )

    precondition, precondition_stats = rv.build_external_precondition_records(
        operations, "read-pages"
    )
    warmup = rv.build_mqsim_warmup_operations(
        profile, precondition, profile.page_program_latency_lsb
    )
    settle_ns = max(
        1_000_000,
        profile.block_erase_latency * 2,
        profile.page_program_latency_lsb * profile.page_no_per_block * 2,
    )
    shift_ns = len(warmup) * profile.page_program_latency_lsb + settle_ns
    case = rv.GeneratedCase(
        name="complex_read_1k_ideal_mapping",
        operations=operations,
        flash_precondition_records=precondition,
        mqsim_warmup_operations=warmup,
        mqsim_main_time_shift_ns=shift_ns,
        mqsim_cache_mode="TURNED_OFF",
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        flashsim_runtime_overrides={"static_wl_enabled": False},
        latency_alignment=True,
        direct_latency_compare=True,
        trace_kind="complex-read",
        external_trace_stats={
            "source_request_count": len(operations),
            "precondition": precondition_stats,
            "mqsim_warmup_request_count": len(warmup),
            "mqsim_main_time_shift_ns": shift_ns,
        },
    )
    paths = rv.write_case_inputs(case, profile, args.case_dir)
    flash = rv.run_flashsim(
        paths,
        profile,
        rv.choose_python(None),
        args.timeout,
        cache_bypass=True,
        plane_allocation="CWDP",
        no_timeline=False,
        fast_report=False,
    )
    mqsim = rv.run_mqsim(paths, rv.MQSIM_ROOT / "MQSim", args.timeout)
    result = {
        "profile": profile.name,
        "case": case.name,
        "main_requests": len(operations),
        "precondition_pages": len(precondition or []),
        "mqsim_warmup_requests": len(warmup),
        "mqsim_main_time_shift_ns": shift_ns,
        "paths": {key: str(value) for key, value in paths.items()},
        "flashsim_run": flash,
        "mqsim_run": mqsim,
        "flashsim_report": rv.parse_flashsim_report(Path(flash["report"])),
        "mqsim_report": rv.parse_mqsim_report(
            Path(mqsim["report"]), Path(mqsim["stdout_log"])
        ),
    }
    (args.case_dir / "complex_summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "flashsim_exit": flash["exit_code"],
        "mqsim_exit": mqsim["exit_code"],
        "precondition_pages": len(precondition or []),
        "main_time_shift_ns": shift_ns,
    }, indent=2))
    return 0 if flash["exit_code"] == 0 and mqsim["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
