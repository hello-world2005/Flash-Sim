#!/usr/bin/env python3
"""Generate equivalent full-page, read-only traces for Flash-Sim and MQSim."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SECTORS_PER_PAGE = 64  # Both simulators use 64-B sectors and 4-KiB pages.


def generate(count: int, gap_ns: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"read_only_{count // 1000}k_64b"
    flash_path = output_dir / f"{stem}_flashsim.json"
    mqsim_path = output_dir / f"{stem}_mqsim.trace"

    flash_records = []
    mqsim_lines = []
    for index in range(count):
        time_ns = index * gap_ns
        start_sector = index * SECTORS_PER_PAGE
        flash_records.append(
            {
                "type": "read",
                "time": time_ns,
                "start_lha": start_sector,
                "size": SECTORS_PER_PAGE,
                "stream_id": 0,
            }
        )
        mqsim_lines.append(
            f"{time_ns} 0 {start_sector} {SECTORS_PER_PAGE} 1"
        )

    flash_path.write_text(json.dumps(flash_records, indent=2) + "\n", encoding="utf-8")
    mqsim_path.write_text("\n".join(mqsim_lines) + "\n", encoding="utf-8")
    print(f"generated {count} reads: {flash_path}")
    print(f"generated {count} reads: {mqsim_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gap-ns", type=int, default=1_000_000)
    args = parser.parse_args()
    for count in (1_000, 10_000):
        generate(count, args.gap_ns, args.output_dir)


if __name__ == "__main__":
    main()
