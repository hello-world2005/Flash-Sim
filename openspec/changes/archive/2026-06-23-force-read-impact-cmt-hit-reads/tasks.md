## 1. Request Latency Report Export

- [x] 1.1 Modify `flash_sim/request_latency_report.py::RequestLatencyRecorder.export` to include a top-level `mapping_resolution_counts` object in every JSON request record.
- [x] 1.2 Modify or add tests in `test_script/test_request_latency_report.py` to verify CMT-hit reads, mapping-read reads, and non-mapping requests export the expected mapping-resolution counts.

## 2. Read-Impact CMT-Hit Experiment Setup

- [x] 2.1 Add helper functions in `test_script/request_resource_contention_experiments.py` to compute all LPAs and sector ranges touched by read-impact read commands.
- [x] 2.2 Add helper functions in `test_script/request_resource_contention_experiments.py` to validate touched read sectors against `precondition_data.json` records and reject missing or invalid sectors.
- [x] 2.3 Add a CMT warm-capacity helper in `test_script/request_resource_contention_experiments.py` that mirrors runtime preconditioning capacity from `CMT_SIZE` and the event-runtime preconditioning ratio default.
- [x] 2.4 Add a helper in `test_script/request_resource_contention_experiments.py` to write a minimal read-impact precondition JSON containing only the touched LPA records.
- [x] 2.5 Modify `test_script/request_resource_contention_experiments.py::run_read_impact_comparison` to generate the minimal precondition JSON and pass it as `pre_trace` to both baseline and compute-contention simulator runs.

## 3. Read-Impact Report Validation

- [x] 3.1 Add report-validation helpers in `test_script/request_resource_contention_experiments.py` that require every compared `READ` request to have `mapping_read == 0` and `cmt_hit` equal to total mapping lookups.
- [x] 3.2 Modify `test_script/request_resource_contention_experiments.py::run_read_impact_comparison` to run CMT-hit report validation before writing JSON/CSV comparison output.
- [x] 3.3 Modify or add tests in `test_script/test_request_resource_contention_experiments.py` for default read precondition generation, custom read validation failures, mapping-read report rejection, and successful all-CMT-hit comparison output.

## 4. Verification

- [x] 4.1 Run `pytest test_script/test_request_latency_report.py`.
- [x] 4.2 Run `pytest test_script/test_request_resource_contention_experiments.py`.
- [x] 4.3 Run the read-impact experiment or a focused stubbed equivalent and confirm the generated comparison contains only reads whose `mapping_resolution_counts.mapping_read` is `0`.
