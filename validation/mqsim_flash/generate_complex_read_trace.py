#!/usr/bin/env python3
"""Generate a deterministic mixed-size, contended, read-only 64-B-sector trace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SECTORS_PER_PAGE = 64
SIZE_PATTERN = (1, 4, 8, 16, 32, 63, 64, 65, 96, 128)
OFFSET_PATTERN = (0, 1, 7, 15, 31, 63)


def request_gap(index: int) -> int:
    position = index % 20
    if position == 0:
        return 50_000 if index else 0
    if position <= 7:
        return 200
    if position <= 13:
        return 1_000
    return 10_000


def generate(count: int) -> tuple[list[dict], list[str], list[dict]]:
    records: list[dict] = []
    mqsim_lines: list[str] = []
    manifest: list[dict] = []
    time_ns = 0
    for index in range(count):
        time_ns += request_gap(index)
        # Rotate the patterns once per 20-request burst so request size and
        # page offset are not correlated with a fixed burst position.
        burst = index // 20
        size = SIZE_PATTERN[(index * 7 + burst) % len(SIZE_PATTERN)]
        offset = OFFSET_PATTERN[(index * 5 + burst) % len(OFFSET_PATTERN)]

        # Most requests revisit a compact hot region; every fourth request is
        # spread over a larger region. This creates both die/channel contention
        # and enough address diversity to exercise segmentation.
        if index % 4:
            base_page = (index * 17) % 256
        else:
            base_page = (index * 73) % 4096
        start_sector = base_page * SECTORS_PER_PAGE + offset
        page_count = (offset + size + SECTORS_PER_PAGE - 1) // SECTORS_PER_PAGE
        aligned_full_page = offset == 0 and size == SECTORS_PER_PAGE
        crosses_page = page_count > 1

        records.append(
            {
                "type": "read",
                "time": time_ns,
                "start_lha": start_sector,
                "size": size,
                "stream_id": 0,
            }
        )
        mqsim_lines.append(f"{time_ns} 0 {start_sector} {size} 1")
        manifest.append(
            {
                "index": index,
                "time_ns": time_ns,
                "start_sector": start_sector,
                "size_sectors": size,
                "size_bytes": size * 64,
                "offset_in_page_sectors": offset,
                "page_count": page_count,
                "crosses_page": crosses_page,
                "aligned_full_page": aligned_full_page,
                "burst_position": index % 20,
            }
        )
    return records, mqsim_lines, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    records, mqsim_lines, manifest = generate(args.count)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"complex_read_{args.count // 1000}k_64b"
    (args.output_dir / f"{stem}_flashsim.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / f"{stem}_mqsim.trace").write_text(
        "\n".join(mqsim_lines) + "\n", encoding="utf-8"
    )
    (args.output_dir / f"{stem}_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
