#!/usr/bin/env python3
"""Join complex-read reports and add Flash-Sim's reported PCIe payload time."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def stats(values: list[int]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "average_ns": sum(values) / len(values),
        "p50_ns": percentile(values, 0.50),
        "p95_ns": percentile(values, 0.95),
        "p99_ns": percentile(values, 0.99),
        "min_ns": min(values),
        "max_ns": max(values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--flash-detail", type=Path, required=True)
    parser.add_argument("--mqsim", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    metadata = {int(item["time_ns"]): item for item in manifest}
    with args.flash_detail.open(newline="", encoding="utf-8") as source:
        flash_rows = list(csv.DictReader(source))
    with args.mqsim.open(newline="", encoding="utf-8") as source:
        mqsim_rows = list(csv.DictReader(source))
    mqsim_by_arrival = {int(row["ArrivalTime"]): row for row in mqsim_rows}

    joined = []
    for flash in flash_rows:
        arrival = int(flash["Issue Time"]) - int(flash["Time in SQ"])
        mqsim = mqsim_by_arrival[arrival]
        item = metadata[arrival]
        flash_original = int(flash["Finish Time"]) - arrival
        compensation = int(flash["PCIe Xfer (Data)"])
        flash_adjusted = flash_original + compensation
        mqsim_latency = int(mqsim["ExecTime"])
        joined.append(
            {
                "RequestIndex": item["index"],
                "ArrivalTime": arrival,
                "SizeBytes": item["size_bytes"],
                "OffsetInPageSectors": item["offset_in_page_sectors"],
                "PagesTouched": item["page_count"],
                "CrossesPage": int(item["crosses_page"]),
                "BurstPosition": item["burst_position"],
                "FlashOriginalExecTime": flash_original,
                "PCIePayloadCompensation": compensation,
                "FlashAdjustedExecTime": flash_adjusted,
                "MQSimExecTime": mqsim_latency,
                "MQSimMinusFlashAdjusted": mqsim_latency - flash_adjusted,
            }
        )

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(joined[0]))
        writer.writeheader()
        writer.writerows(joined)

    groups: dict[str, list[dict]] = {
        "overall": joined,
        "within_page": [row for row in joined if not row["CrossesPage"]],
        "cross_page": [row for row in joined if row["CrossesPage"]],
        "burst_200ns": [row for row in joined if 1 <= row["BurstPosition"] <= 7],
        "burst_1us": [row for row in joined if 8 <= row["BurstPosition"] <= 13],
        "gap_10us": [row for row in joined if 14 <= row["BurstPosition"] <= 19],
        "gap_50us": [row for row in joined if row["BurstPosition"] == 0],
    }
    for pages in (1, 2, 3):
        groups[f"pages_{pages}"] = [row for row in joined if row["PagesTouched"] == pages]
    for size in sorted({int(row["SizeBytes"]) for row in joined}):
        groups[f"size_{size}B"] = [row for row in joined if row["SizeBytes"] == size]

    result = {}
    for name, rows in groups.items():
        result[name] = {
            "flash_original": stats([int(row["FlashOriginalExecTime"]) for row in rows]),
            "payload_compensation": stats([int(row["PCIePayloadCompensation"]) for row in rows]),
            "flash_adjusted": stats([int(row["FlashAdjustedExecTime"]) for row in rows]),
            "mqsim": stats([int(row["MQSimExecTime"]) for row in rows]),
            "mqsim_minus_flash_adjusted": stats(
                [int(row["MQSimMinusFlashAdjusted"]) for row in rows]
            ),
        }
    args.output_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
