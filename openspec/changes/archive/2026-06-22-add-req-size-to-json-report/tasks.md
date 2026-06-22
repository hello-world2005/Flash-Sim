## 1. Report Export

- [x] 1.1 Confirm `flash_sim/request_latency_report.py::RequestLatencyState` stores the original `Request.size`.
- [x] 1.2 Confirm `flash_sim/request_latency_report.py::RequestLatencyRecorder._ensure_request` refreshes `RequestLatencyState.size` from the current `Request.size`.
- [x] 1.3 Confirm `flash_sim/request_latency_report.py::RequestLatencyRecorder.export` writes `size` as a top-level field for each JSON request record.

## 2. Verification

- [x] 2.1 Modify `test_script/test_request_latency_report_e2e.py` to assert generated JSON report request entries include the trace `size`.
- [x] 2.2 Run the focused request latency report tests.
