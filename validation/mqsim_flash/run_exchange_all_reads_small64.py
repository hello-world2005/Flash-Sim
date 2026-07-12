#!/usr/bin/env python3
"""Convert every Exchange read and replay it on finite-CMT small64 SSDs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import run_validation as rv


def convert(source: Path, page_limit: int, occupancy: float, pressure_window: int | None):
    # Keep each source disk in a separate logical partition.  Modulo conversion
    # preserves the within-request sector offset and never lets an I/O wrap.
    disk_count = 10
    usable_pages = int(page_limit * occupancy)
    partition_sectors = (usable_pages * rv.FLASHSIM_SECTORS_PER_PAGE) // disk_count
    operations = []
    touched_pages: set[int] = set()
    stats = Counter()
    source_min = None
    source_max_exclusive = 0
    first_timestamp = None
    last_timestamp = None

    source_reads = []
    with source.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.reader(handle, skipinitialspace=True):
            if not row or row[0].strip() != "DiskRead":
                continue
            stats["source_read_rows"] += 1
            try:
                timestamp = int(row[1].strip(), 0)
                byte_offset = int(row[5].strip(), 0)
                byte_size = int(row[6].strip(), 0)
                disk = int(row[8].strip(), 0)
            except (IndexError, ValueError):
                stats["malformed"] += 1
                continue
            if byte_size <= 0 or byte_offset < 0:
                stats["nonpositive"] += 1
                continue
            if byte_offset % rv.FLASHSIM_SECTOR_SIZE_BYTES or byte_size % rv.FLASHSIM_SECTOR_SIZE_BYTES:
                stats["not_64b_aligned"] += 1
                continue
            source_reads.append((timestamp, byte_offset, byte_size, disk))

    stats["valid_source_reads"] = len(source_reads)
    window_start = 0
    if pressure_window is not None:
        if pressure_window <= 0 or pressure_window > len(source_reads):
            raise ValueError("pressure window must be within the valid source read count")
        window_start = min(
            range(len(source_reads) - pressure_window + 1),
            key=lambda index: source_reads[index + pressure_window - 1][0] - source_reads[index][0],
        )
        source_reads = source_reads[window_start:window_start + pressure_window]
    stats["selected_reads"] = len(source_reads)

    for selected_index, (timestamp, byte_offset, byte_size, disk) in enumerate(source_reads):
            size_sectors = byte_size // rv.FLASHSIM_SECTOR_SIZE_BYTES
            if size_sectors >= partition_sectors:
                raise ValueError(f"read is larger than its small64 disk partition: {byte_size} B")
            source_sector = byte_offset // rv.FLASHSIM_SECTOR_SIZE_BYTES
            disk_base = disk * partition_sectors
            local_limit = partition_sectors - size_sectors + 1
            mapped_sector = disk_base + source_sector % local_limit
            if mapped_sector != source_sector:
                stats["remapped"] += 1
            if first_timestamp is None:
                first_timestamp = timestamp
            time_ns = (timestamp - first_timestamp) * 1_000
            last_timestamp = timestamp
            start_page = mapped_sector // rv.FLASHSIM_SECTORS_PER_PAGE
            end_page = (mapped_sector + size_sectors - 1) // rv.FLASHSIM_SECTORS_PER_PAGE
            touched_pages.update(range(start_page, end_page + 1))
            operations.append({
                "type": "read", "time": time_ns, "page_id": start_page,
                "flashsim_start_lha": mapped_sector, "flashsim_size": size_sectors,
                "mqsim_start_sector": mapped_sector, "mqsim_size_sectors": size_sectors,
                "phase": "main", "source_index": window_start + selected_index,
                "source_disk": disk, "source_byte_offset": byte_offset,
            })
            source_min = byte_offset if source_min is None else min(source_min, byte_offset)
            source_max_exclusive = max(source_max_exclusive, byte_offset + byte_size)
            stats["emitted"] += 1

    manifest = {
        **stats,
        "source": str(source), "source_time_unit": "microsecond",
        "output_time_unit": "nanosecond", "first_source_timestamp": first_timestamp,
        "last_source_timestamp": last_timestamp,
        "source_min_byte": source_min, "source_max_byte_exclusive": source_max_exclusive,
        "small64_logical_page_limit": page_limit, "occupancy_ratio": occupancy,
        "usable_pages": usable_pages, "partition_sectors": partition_sectors,
        "unique_prefilled_pages": len(touched_pages),
        "mapped_min_sector": min(op["flashsim_start_lha"] for op in operations),
        "mapped_max_sector_exclusive": max(op["flashsim_start_lha"] + op["flashsim_size"] for op in operations),
        "mapping": "ten disk-id partitions; start=(disk*partition)+(source_sector % (partition-size+1))",
        "pressure_window_size": pressure_window,
        "pressure_window_source_start_index": window_start if pressure_window is not None else None,
        "pressure_window_span_source_units": (
            source_reads[-1][0] - source_reads[0][0] if pressure_window is not None else None
        ),
    }
    return operations, sorted(touched_pages), manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=rv.FLASH_SIM_ROOT / "traces/Exchange.12-12-2007.02-39-PM.trace.csv")
    parser.add_argument("--case-dir", type=Path, default=SCRIPT_DIR / "out/exchange_all_reads_small64")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--occupancy", type=float, default=0.80)
    parser.add_argument("--invalid-ratio", type=float, default=0.25,
                        help="Fraction of written physical pages made invalid in Flash-Sim preconditioning")
    parser.add_argument("--pressure-window", type=int, default=None,
                        help="Select the consecutive N-read window with the shortest timestamp span")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not 0 < args.occupancy < 1 or not 0 <= args.invalid_ratio < 1:
        raise SystemExit("occupancy must be in (0,1), invalid-ratio in [0,1)")

    profile = rv.PROFILES["flashsim-event-small-finite-cmt-aligned"]
    operations, pages, manifest = convert(
        args.source.resolve(), rv.external_logical_page_limit(profile), args.occupancy,
        args.pressure_window,
    )
    precondition = [rv.make_precondition_record(page) for page in pages]
    warmup_gap_ns = profile.page_program_latency_lsb
    warmup = rv.build_mqsim_warmup_operations(profile, precondition, warmup_gap_ns)
    # Re-write a deterministic quarter of the pages so MQSim also starts with
    # stale physical pages.  The final mapping remains valid for every read.
    invalidating_warmup = rv.build_mqsim_warmup_operations(
        profile, precondition[:round(len(precondition) * args.invalid_ratio)], warmup_gap_ns
    )
    for index, operation in enumerate(invalidating_warmup, start=len(warmup)):
        operation["time"] = index * warmup_gap_ns
        operation["mqsim_warmup_index"] = index
    warmup.extend(invalidating_warmup)
    settle_ns = max(1_000_000, profile.block_erase_latency * 2)
    main_shift_ns = len(warmup) * warmup_gap_ns + settle_ns
    case = rv.GeneratedCase(
        name="exchange_all_reads_small64_finite_cmt_used",
        operations=operations,
        flash_precondition_records=precondition,
        mqsim_warmup_operations=warmup,
        mqsim_main_time_shift_ns=main_shift_ns,
        mqsim_enabled_preconditioning=False,
        mqsim_initial_occupancy_percentage=0,
        mqsim_cache_mode="TURNED_OFF",
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        flashsim_runtime_overrides={"static_wl_enabled": False},
        latency_alignment=True,
        direct_latency_compare=True,
        trace_kind="exchange-all-reads",
        external_trace_stats=manifest,
    )
    paths = rv.write_case_inputs(case, profile, args.case_dir)
    manifest_path = args.case_dir / "conversion_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.prepare_only:
        print(json.dumps({"paths": {k: str(v) for k, v in paths.items()}, **manifest}, indent=2))
        return 0

    python_bin = rv.choose_python(None)
    old_ratio = os.environ.get("FLASHSIM_EVENT_RUNTIME_VALID_INVALID_RATIO")
    os.environ["FLASHSIM_EVENT_RUNTIME_VALID_INVALID_RATIO"] = str(1.0 - args.invalid_ratio)
    try:
        flash = rv.run_flashsim(paths, profile, python_bin, args.timeout,
                                cache_bypass=True, plane_allocation="CWDP",
                                no_timeline=True, fast_report=True)
    finally:
        if old_ratio is None:
            os.environ.pop("FLASHSIM_EVENT_RUNTIME_VALID_INVALID_RATIO", None)
        else:
            os.environ["FLASHSIM_EVENT_RUNTIME_VALID_INVALID_RATIO"] = old_ratio
    mqsim = rv.run_mqsim(paths, rv.MQSIM_ROOT / "MQSim", args.timeout)
    result = {
        "conversion": manifest,
        "finite_cmt": {"entries": 64, "mqsim_capacity_bytes": profile.cmt_capacity,
                       "ideal_mapping_table": False},
        "used_ssd": {"logical_prefill_pages": len(pages),
                     "mqsim_warmup_writes": len(warmup),
                     "invalid_physical_page_ratio_target": args.invalid_ratio},
        "paths": {k: str(v) for k, v in paths.items()},
        "flashsim_run": flash, "mqsim_run": mqsim,
        "flashsim_report": rv.parse_flashsim_report(Path(flash["report"])),
        "mqsim_report": rv.parse_mqsim_report(Path(mqsim["report"]), Path(mqsim["stdout_log"])),
    }
    (args.case_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"flashsim_exit": flash["exit_code"], "mqsim_exit": mqsim["exit_code"],
                      "reads": len(operations), "prefilled_pages": len(pages)}, indent=2))
    return 0 if flash["exit_code"] == 0 and mqsim["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
