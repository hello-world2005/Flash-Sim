import csv
import io
import json
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from flash_sim.engine import Engine
from flash_sim.request_latency_report import CSV_COLUMN_NAMES


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_CASE_DIR = REPO_ROOT / "test_case"


def _run_engine_and_load_report(trace_content, trace_name):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        trace_path = tmpdir_path / trace_name
        trace_path.write_text(json.dumps(trace_content), encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            random.seed(0)
            engine = Engine()
            engine.Start_simulation(str(trace_path))

        report_path = engine.last_request_latency_report_path
        csv_path = engine.last_request_latency_csv_path
        if report_path is None or not report_path.exists():
            raise AssertionError("expected request latency JSON report to be generated")
        if csv_path is None or not csv_path.exists():
            raise AssertionError("expected request latency CSV report to be generated")
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            return json.loads(report_path.read_text(encoding="utf-8")), csv_rows, buf.getvalue()
        finally:
            report_path.unlink()
            csv_path.unlink()


def _completion_path_sum(csv_row):
    return sum(
        int(csv_row[column])
        for column in (
            CSV_COLUMN_NAMES[3],
            CSV_COLUMN_NAMES[4],
            CSV_COLUMN_NAMES[6],
            CSV_COLUMN_NAMES[7],
            CSV_COLUMN_NAMES[8],
            CSV_COLUMN_NAMES[9],
            CSV_COLUMN_NAMES[10],
        )
    )


class TestRequestLatencyReportEndToEnd(unittest.TestCase):
    def test_mapping_miss_read_exports_additive_completion_path(self):
        trace_content = json.loads((TEST_CASE_DIR / "test_read.json").read_text(encoding="utf-8"))
        report, csv_rows, output = _run_engine_and_load_report(trace_content, "single_read_trace.json")

        self.assertNotIn("Traceback", output)
        self.assertEqual(report["meta"]["request_count"], 1)
        req = report["requests"][0]
        csv_row = csv_rows[0]

        self.assertEqual(req["type"], "READ")
        self.assertEqual(req["size"], trace_content[0]["size"])
        self.assertEqual(req["persistence_status"], "not_applicable")
        self.assertGreater(req["breakdown"]["pcie_host_to_device"], 0)
        self.assertGreater(req["breakdown"]["pcie_device_to_host"], 0)
        self.assertGreater(req["breakdown"]["amu_mapping_wait"], 0)
        self.assertGreater(req["breakdown"]["phy_cmd_addr"], 0)
        self.assertGreater(req["breakdown"]["phy_array_exec"], 0)
        self.assertGreater(req["breakdown"]["phy_data_out"], 0)
        self.assertEqual(csv_row[CSV_COLUMN_NAMES[1]], "READ")
        self.assertEqual(csv_row[CSV_COLUMN_NAMES[5]], "No")
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[6]]), 0)
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[8]]), 0)
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[9]]), 0)
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[10]]), 0)
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[11]]), 0)
        self.assertEqual(
            int(csv_row[CSV_COLUMN_NAMES[2]]) - int(csv_row[CSV_COLUMN_NAMES[0]]),
            _completion_path_sum(csv_row),
        )

    def test_mapping_dependency_queue_is_absorbed_into_mapping_column(self):
        trace_content = [
            {"type": "read", "time": 0, "start_lha": 4508800, "size": 1},
            {"type": "read", "time": 0, "start_lha": 4508800, "size": 1},
        ]
        report, csv_rows, output = _run_engine_and_load_report(trace_content, "read_queue_trace.json")

        self.assertNotIn("Traceback", output)
        self.assertEqual(report["meta"]["request_count"], 2)
        self.assertTrue(
            any(req["breakdown"]["amu_mapping_wait"] > 0 for req in report["requests"])
        )
        self.assertTrue(
            any(req["breakdown"]["tsu_queue_wait"] > 0 for req in report["requests"])
        )
        self.assertEqual(len(csv_rows), 2)
        first_req, second_req = report["requests"]
        first_row, second_row = csv_rows
        self.assertEqual(first_req["type"], "READ")
        self.assertEqual(second_req["type"], "READ")
        self.assertEqual(second_req["breakdown"]["amu_mapping_wait"], 0)
        self.assertGreater(second_req["breakdown"]["tsu_queue_wait"], 0)
        self.assertGreater(int(second_row[CSV_COLUMN_NAMES[6]]), int(first_row[CSV_COLUMN_NAMES[6]]))
        self.assertEqual(int(second_row[CSV_COLUMN_NAMES[7]]), 0)
        self.assertTrue(
            all(
                int(row[CSV_COLUMN_NAMES[2]]) - int(row[CSV_COLUMN_NAMES[0]]) == _completion_path_sum(row)
                for row in csv_rows
            )
        )

    def test_write_report_uses_host_visible_completion_path(self):
        trace_content = json.loads((TEST_CASE_DIR / "test_write.json").read_text(encoding="utf-8"))
        report, csv_rows, output = _run_engine_and_load_report(trace_content, "single_write_trace.json")

        self.assertNotIn("Traceback", output)
        self.assertEqual(report["meta"]["request_count"], 1)
        req = report["requests"][0]
        csv_row = csv_rows[0]

        self.assertEqual(req["type"], "WRITE")
        self.assertGreater(req["host_total_latency"], 0)
        self.assertGreater(req["breakdown"]["pcie_host_to_device"], 0)
        self.assertGreater(req["breakdown"]["pcie_device_to_host"], 0)
        self.assertEqual(req["persistence_status"], "persisted")
        self.assertGreater(req["persistence_total_latency"], req["host_total_latency"])
        self.assertGreater(req["persistence_breakdown"]["phy_cmd_addr"], 0)
        self.assertGreater(req["persistence_breakdown"]["phy_data_in"], 0)
        self.assertGreater(req["persistence_breakdown"]["phy_array_exec"], 0)
        self.assertEqual(csv_row[CSV_COLUMN_NAMES[1]], "WRITE")
        self.assertEqual(int(csv_row[CSV_COLUMN_NAMES[6]]), 0)
        self.assertEqual(int(csv_row[CSV_COLUMN_NAMES[7]]), 0)
        self.assertEqual(int(csv_row[CSV_COLUMN_NAMES[8]]), 0)
        self.assertEqual(int(csv_row[CSV_COLUMN_NAMES[9]]), 0)
        self.assertGreater(int(csv_row[CSV_COLUMN_NAMES[10]]), 0)
        self.assertEqual(int(csv_row[CSV_COLUMN_NAMES[11]]), 0)
        self.assertEqual(
            int(csv_row[CSV_COLUMN_NAMES[2]]) - int(csv_row[CSV_COLUMN_NAMES[0]]),
            _completion_path_sum(csv_row),
        )

    def test_search_and_compute_rows_keep_cache_not_applicable_and_add_compute_rows(self):
        trace_content = json.loads((TEST_CASE_DIR / "test_search_compute.json").read_text(encoding="utf-8"))
        report, csv_rows, output = _run_engine_and_load_report(trace_content, "search_compute_trace.json")

        self.assertNotIn("Traceback", output)
        self.assertEqual(report["meta"]["request_count"], 4)
        self.assertEqual(len(csv_rows), 4)
        self.assertEqual(
            [row[CSV_COLUMN_NAMES[1]] for row in csv_rows],
            ["SEARCH", "COMPUTE", "SEARCH", "COMPUTE"],
        )
        self.assertEqual(
            [req["size"] for req in report["requests"]],
            [trace_req["size"] for trace_req in trace_content],
        )
        self.assertTrue(all(row[CSV_COLUMN_NAMES[5]] == "/" for row in csv_rows))
        self.assertTrue(all(int(row[CSV_COLUMN_NAMES[6]]) == 0 for row in csv_rows))
        compute_rows = [row for row in csv_rows if row[CSV_COLUMN_NAMES[1]] == "COMPUTE"]
        self.assertTrue(all(int(row[CSV_COLUMN_NAMES[10]]) > 0 for row in compute_rows))
        self.assertTrue(all(int(row[CSV_COLUMN_NAMES[11]]) > 0 for row in compute_rows))
        self.assertTrue(
            all(
                int(row[CSV_COLUMN_NAMES[2]]) - int(row[CSV_COLUMN_NAMES[0]]) == _completion_path_sum(row)
                for row in compute_rows
            )
        )


if __name__ == "__main__":
    unittest.main()
