import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flash_sim.request_latency_report import CSV_COLUMN_NAMES


def _completion_path_sum(row):
    return sum(
        int(row[CSV_COLUMN_NAMES[index]])
        for index in (3, 4, 6, 7, 8, 9, 10)
    )


class TestReadWriteTrace(unittest.TestCase):
    def test_main_trace_read_after_write_completes_without_mapping_error(self):
        repo_root = Path(__file__).resolve().parents[1]
        source_trace = repo_root / "test_case" / "test_read_write.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            trace_path = tmpdir_path / "read_write_main_trace.json"
            trace_path.write_text(source_trace.read_text(encoding="utf-8"), encoding="utf-8")
            log_path = tmpdir_path / "read_write_main_trace.log"
            report_path = repo_root / "report" / "read_write_main_trace_request_latency.json"
            csv_path = repo_root / "report" / "read_write_main_trace_request_latency.csv"

            try:
                proc = subprocess.run(
                    [sys.executable, "flash_sim/main.py"],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    env={
                        **os.environ,
                        "FLASH_SIM_INPUT_JSON": str(trace_path),
                        "FLASH_SIM_MERGED_LOG": str(log_path),
                        "FLASH_SIM_MIRROR_CONSOLE": "0",
                    },
                )

                log_output = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                combined = proc.stdout + proc.stderr + log_output
                self.assertEqual(proc.returncode, 0, msg=combined)
                self.assertNotIn("Read request accessing non-existing mapping page", combined)
                self.assertNotIn("Error:", combined)
                self.assertIn("Request latency report:", combined)
                self.assertTrue(log_path.exists(), msg="expected merged log to be generated")
                self.assertTrue(report_path.exists(), msg="expected request latency report to be generated")
                self.assertTrue(csv_path.exists(), msg="expected request latency csv to be generated")

                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["meta"]["request_count"], 2)
                self.assertEqual(
                    [req["type"] for req in report["requests"]],
                    ["WRITE", "READ"],
                )
                read_req = report["requests"][1]
                self.assertEqual(read_req["data_cache_status"], "full_hit")
                self.assertEqual(read_req["breakdown"]["phy_cmd_addr"], 0)
                self.assertEqual(read_req["breakdown"]["phy_array_exec"], 0)

                with csv_path.open("r", encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[1][CSV_COLUMN_NAMES[1]], "READ")
                self.assertEqual(rows[1][CSV_COLUMN_NAMES[5]], "是")
                self.assertEqual(int(rows[1][CSV_COLUMN_NAMES[6]]), 0)
                self.assertEqual(int(rows[1][CSV_COLUMN_NAMES[7]]), 0)
                self.assertEqual(int(rows[1][CSV_COLUMN_NAMES[8]]), 0)
                self.assertEqual(int(rows[1][CSV_COLUMN_NAMES[9]]), 0)
                self.assertEqual(
                    int(rows[1][CSV_COLUMN_NAMES[2]]) - int(rows[1][CSV_COLUMN_NAMES[0]]),
                    _completion_path_sum(rows[1]),
                )
            finally:
                if report_path.exists():
                    report_path.unlink()
                if csv_path.exists():
                    csv_path.unlink()


if __name__ == "__main__":
    unittest.main()
