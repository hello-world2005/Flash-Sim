#!/usr/bin/env python3
"""Convert SNIA Microsoft Exchange traces to MQSim and Flash-Sim inputs.

The Exchange archive stores Windows ETW CSV files inside gzip members.  The
maintained MQSim-test build and Flash-Sim event path both use 64-byte host
sectors.  This converter keeps both outputs derived from the same byte-addressed
DiskRead/DiskWrite records.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import gzip
import io
import json
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


MQSIM_SECTOR_BYTES = 64
FLASHSIM_SECTOR_BYTES = 64
DEFAULT_PAGE_BYTES = 4096


@dataclass
class ConvertStats:
    input_path: str
    members_seen: int = 0
    members_processed: list[str] = field(default_factory=list)
    rows_seen: int = 0
    disk_events_seen: int = 0
    emitted: int = 0
    read_count: int = 0
    write_count: int = 0
    skipped_header: int = 0
    skipped_malformed: int = 0
    skipped_disk_filter: int = 0
    skipped_zero_size: int = 0
    skipped_sector_alignment: int = 0
    skipped_page_alignment: int = 0
    first_input_timestamp: int | None = None
    last_input_timestamp: int | None = None
    first_output_time_ns: int | None = None
    last_output_time_ns: int | None = None
    min_mqsim_lba_64: int | None = None
    max_mqsim_lba_64_exclusive: int | None = None
    min_flash_lha_64: int | None = None
    max_flash_lha_64_exclusive: int | None = None


def parse_int(value: str) -> int:
    return int(value.strip(), 0)


def iter_members(input_path: Path, pattern: str, limit: int | None) -> Iterator[tuple[str, io.BufferedReader]]:
    if input_path.suffix == ".tar":
        count = 0
        with tarfile.open(input_path, "r") as archive:
            for member in sorted(archive.getmembers(), key=lambda item: item.name):
                if not member.isfile() or not fnmatch.fnmatch(member.name, pattern):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                yield member.name, extracted
                count += 1
                if limit is not None and count >= limit:
                    break
    else:
        yield input_path.name, input_path.open("rb")


def iter_disk_rows(binary_stream: io.BufferedReader) -> Iterator[list[str]]:
    with gzip.GzipFile(fileobj=binary_stream) as gzip_stream:
        text_stream = io.TextIOWrapper(gzip_stream, encoding="utf-8", errors="replace", newline="")
        reader = csv.reader(text_stream, skipinitialspace=True)
        for row in reader:
            if not row:
                continue
            event = row[0].strip()
            if event in {"DiskRead", "DiskWrite"}:
                yield row


def update_range(current_min: int | None, current_max: int | None, start: int, size: int) -> tuple[int, int]:
    end = start + size
    if current_min is None or start < current_min:
        current_min = start
    if current_max is None or end > current_max:
        current_max = end
    return current_min, current_max


def convert(
    input_path: Path,
    mqsim_output: Path,
    flashsim_output: Path,
    manifest_output: Path,
    *,
    member_pattern: str,
    member_limit: int | None,
    max_requests: int | None,
    disk_id: int | None,
    page_aligned_only: bool,
    page_bytes: int,
    source_time_unit: str,
    normalize_time: bool,
) -> ConvertStats:
    stats = ConvertStats(input_path=str(input_path))
    time_multiplier = {"ns": 1, "us": 1_000}[source_time_unit]

    mqsim_output.parent.mkdir(parents=True, exist_ok=True)
    flashsim_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)

    with mqsim_output.open("w", encoding="utf-8") as mqsim_file, flashsim_output.open(
        "w", encoding="utf-8"
    ) as flash_file:
        flash_file.write("[\n")
        first_flash_record = True

        for member_name, member_stream in iter_members(input_path, member_pattern, member_limit):
            stats.members_seen += 1
            stats.members_processed.append(member_name)
            with member_stream:
                for row in iter_disk_rows(member_stream):
                    stats.rows_seen += 1
                    if len(row) < 9 or row[1].strip() == "TimeStamp":
                        stats.skipped_header += 1
                        continue

                    try:
                        event = row[0].strip()
                        timestamp = parse_int(row[1])
                        byte_offset = parse_int(row[5])
                        io_size = parse_int(row[6])
                        disk_num = parse_int(row[8])
                    except ValueError:
                        stats.skipped_malformed += 1
                        continue

                    stats.disk_events_seen += 1

                    if disk_id is not None and disk_num != disk_id:
                        stats.skipped_disk_filter += 1
                        continue
                    if io_size <= 0:
                        stats.skipped_zero_size += 1
                        continue
                    if byte_offset % FLASHSIM_SECTOR_BYTES or io_size % FLASHSIM_SECTOR_BYTES:
                        stats.skipped_sector_alignment += 1
                        continue
                    if byte_offset % MQSIM_SECTOR_BYTES or io_size % MQSIM_SECTOR_BYTES:
                        stats.skipped_sector_alignment += 1
                        continue
                    if page_aligned_only and (byte_offset % page_bytes or io_size % page_bytes):
                        stats.skipped_page_alignment += 1
                        continue

                    if stats.first_input_timestamp is None:
                        stats.first_input_timestamp = timestamp
                    stats.last_input_timestamp = timestamp

                    base_timestamp = stats.first_input_timestamp if normalize_time else 0
                    time_ns = (timestamp - base_timestamp) * time_multiplier
                    mqsim_start_lba = byte_offset // MQSIM_SECTOR_BYTES
                    mqsim_size = io_size // MQSIM_SECTOR_BYTES
                    flashsim_start_lha = byte_offset // FLASHSIM_SECTOR_BYTES
                    flashsim_size = io_size // FLASHSIM_SECTOR_BYTES
                    request_type = "read" if event == "DiskRead" else "write"
                    mqsim_type = 1 if request_type == "read" else 0

                    mqsim_file.write(
                        f"{time_ns} {disk_num if disk_id is None else 0} "
                        f"{mqsim_start_lba} {mqsim_size} {mqsim_type}\n"
                    )

                    flash_record = {
                        "type": request_type,
                        "time": time_ns,
                        "start_lha": flashsim_start_lha,
                        "size": flashsim_size,
                        "stream_id": disk_num if disk_id is None else 0,
                    }
                    if not first_flash_record:
                        flash_file.write(",\n")
                    flash_file.write(json.dumps(flash_record, separators=(",", ":")))
                    first_flash_record = False

                    stats.emitted += 1
                    if request_type == "read":
                        stats.read_count += 1
                    else:
                        stats.write_count += 1
                    stats.first_output_time_ns = time_ns if stats.first_output_time_ns is None else stats.first_output_time_ns
                    stats.last_output_time_ns = time_ns
                    stats.min_mqsim_lba_64, stats.max_mqsim_lba_64_exclusive = update_range(
                        stats.min_mqsim_lba_64,
                        stats.max_mqsim_lba_64_exclusive,
                        mqsim_start_lba,
                        mqsim_size,
                    )
                    stats.min_flash_lha_64, stats.max_flash_lha_64_exclusive = update_range(
                        stats.min_flash_lha_64,
                        stats.max_flash_lha_64_exclusive,
                        flashsim_start_lha,
                        flashsim_size,
                    )

                    if max_requests is not None and stats.emitted >= max_requests:
                        break
                if max_requests is not None and stats.emitted >= max_requests:
                    break

        flash_file.write("\n]\n")

    manifest = asdict(stats)
    manifest.update(
        {
            "mqsim_output": str(mqsim_output),
            "flashsim_output": str(flashsim_output),
            "member_pattern": member_pattern,
            "member_limit": member_limit,
            "max_requests": max_requests,
            "disk_id": disk_id,
            "page_aligned_only": page_aligned_only,
            "page_bytes": page_bytes,
            "source_time_unit": source_time_unit,
            "output_time_unit": "NANOSECOND",
            "normalize_time": normalize_time,
            "mqsim_sector_bytes": MQSIM_SECTOR_BYTES,
            "flashsim_sector_bytes": FLASHSIM_SECTOR_BYTES,
        }
    )
    manifest_output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Exchange-Server-Traces.tar or one .trace.csv.gz file")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--name", default="exchange")
    parser.add_argument("--member-pattern", default="Exchange-Server-Traces/Exchange/*.trace.csv.gz")
    parser.add_argument("--member-limit", type=int, default=None)
    parser.add_argument("--max-requests", type=int, default=50_000)
    parser.add_argument("--disk-id", type=int, default=0)
    parser.add_argument("--allow-partial", action="store_true", help="Keep sector-aligned partial-page requests")
    parser.add_argument("--page-bytes", type=int, default=DEFAULT_PAGE_BYTES)
    parser.add_argument("--source-time-unit", choices=("ns", "us"), default="us")
    parser.add_argument("--no-normalize-time", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    page_suffix = "sector" if args.allow_partial else "page"
    disk_suffix = "all-disks" if args.disk_id is None else f"disk{args.disk_id}"
    prefix = f"{args.name}_{disk_suffix}_{page_suffix}_{args.max_requests or 'all'}"
    mqsim_output = args.output_dir / f"{prefix}_mqsim.trace"
    flashsim_output = args.output_dir / f"{prefix}_flashsim.json"
    manifest_output = args.output_dir / f"{prefix}_manifest.json"
    stats = convert(
        args.input,
        mqsim_output,
        flashsim_output,
        manifest_output,
        member_pattern=args.member_pattern,
        member_limit=args.member_limit,
        max_requests=args.max_requests,
        disk_id=args.disk_id,
        page_aligned_only=not args.allow_partial,
        page_bytes=args.page_bytes,
        source_time_unit=args.source_time_unit,
        normalize_time=not args.no_normalize_time,
    )
    print(f"emitted={stats.emitted} reads={stats.read_count} writes={stats.write_count}")
    print(f"mqsim={mqsim_output}")
    print(f"flashsim={flashsim_output}")
    print(f"manifest={manifest_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
