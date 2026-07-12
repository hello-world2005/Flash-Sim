#!/usr/bin/env python3
"""Extract and time-align the main phase of an MQSim per-request CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


TIME_FIELDS = ("IssueTime", "FinishTime", "ArrivalTime")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--main-time-shift-ns", type=int, required=True)
    parser.add_argument("--expected-requests", type=int, required=True)
    args = parser.parse_args()

    written = 0
    with args.input_csv.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("input CSV has no header")
        with args.output_csv.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if int(row["ArrivalTime"]) < args.main_time_shift_ns:
                    continue
                for field in TIME_FIELDS:
                    row[field] = str(int(row[field]) - args.main_time_shift_ns)
                writer.writerow(row)
                written += 1

    if written != args.expected_requests:
        raise ValueError(
            f"main-phase request count mismatch: {written} != {args.expected_requests}"
        )
    print(f"Extracted {written} requests: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
