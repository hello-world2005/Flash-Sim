## 1. Report Export Implementation

- [x] 1.1 Modify `flash_sim/request_latency_report.py` to keep the existing JSON export, add CSV path/row formatting helpers, and write a per-request latency table with the required fixed columns.
- [x] 1.2 Modify `flash_sim/FTL.py` to record request-level cache resolution metadata that distinguishes CMT hits from GMT/direct resolution and mapping-read fallback for CSV export.
- [x] 1.3 Modify `flash_sim/engine.py` to export the CSV report alongside the existing JSON report and retain the generated report paths for callers/tests.

## 2. Verification

- [x] 2.1 Update `test_script/test_request_latency_report.py` to verify CSV row derivation, including cache-hit, mapping-time, write persistence-stage flattening, and non-write PCIe return/status timing.
- [x] 2.2 Update `test_script/test_request_latency_report_e2e.py` to verify the simulator emits both JSON and CSV request latency reports for representative traces.
