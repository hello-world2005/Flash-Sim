## 1. Read Stream and Compute Insertion

- [x] 1.1 Modify `test_script/request_resource_contention_experiments.py` constants to add read-impact ratio scan values `[0.1, 0.2, 0.4, 0.8]`, compute-size scan values `[8, 32, 128, 512]`, fixed size-scan ratio `0.2`, and ratio-scan compute size `128`.
- [x] 1.2 Modify `test_script/request_resource_contention_experiments.py::build_default_read_commands` or add a replacement helper to generate page-aligned page reads from the selected CMT-hit precondition records.
- [x] 1.3 Modify `test_script/request_resource_contention_experiments.py::validate_read_commands_for_cmt_hit` to reject read-impact commands that are not page-aligned page reads.
- [x] 1.4 Add `test_script/request_resource_contention_experiments.py` helpers to compute `num_compute_req = max(1, round(num_read_req * ratio))`, select deterministic insertion anchors, and insert compute commands without mutating the filtered read command list.
- [x] 1.5 Modify or replace `test_script/request_resource_contention_experiments.py::build_paired_read_impact_traces` so it can create the baseline trace plus all ratio-scan and request-size-scan traces instead of a single two-compute-prefix contended trace.

## 2. Scan Execution and Result Aggregation

- [x] 2.1 Modify `test_script/request_resource_contention_experiments.py::run_read_impact_comparison` to run the baseline once and run every configured ratio-scan and request-size-scan condition with the same CMT-hit precondition file.
- [x] 2.2 Modify `test_script/request_resource_contention_experiments.py::validate_read_impact_reports_cmt_hits` or add a wrapper so every compute-contention scan report is validated against the baseline read identities and CMT-hit counts.
- [x] 2.3 Add `test_script/request_resource_contention_experiments.py` aggregation helpers that compute average read latency from only `READ` entries in each request latency JSON report.
- [x] 2.4 Add `test_script/request_resource_contention_experiments.py` normalization helpers that produce condition rows with group name, parameter label/value, configured ratio, compute size, read count, compute count, raw average read latency, and normalized latency relative to the baseline.
- [x] 2.5 Modify `test_script/request_resource_contention_experiments.py::write_read_impact_json` and `write_read_impact_csv` or add replacement writers for condition-level read-impact scan output.

## 3. Chart and CLI Output

- [x] 3.1 Add `test_script/request_resource_contention_experiments.py` grouped SVG chart writer for baseline, insertion-ratio scan, and request-size scan groups using blue, orange, and purple bars.
- [x] 3.2 Ensure the grouped chart writer uses smaller intra-group bar gaps, larger inter-group gaps, group labels below the x-axis, a normalized-latency y-axis label, and exactly two-decimal bar value labels.
- [x] 3.3 Modify `test_script/request_resource_contention_experiments.py::_print_read_impact_summary` to print the new condition-level JSON, CSV, and chart paths.
- [x] 3.4 Modify `test_script/request_resource_contention_experiments.py::_parse_args` only as needed to expose or preserve read-impact scan configuration without breaking the existing `--experiment read-impact` workflow.

## 4. Tests

- [x] 4.1 Modify `test_script/test_request_resource_contention_experiments.py::test_paired_read_impact_traces_have_identical_read_portions_and_compute_prefix` or replace it with tests for identical page-read portions across baseline, ratio-scan traces, and request-size-scan traces.
- [x] 4.2 Add tests in `test_script/test_request_resource_contention_experiments.py` for ratio scan compute counts and size `128`, and request-size scan sizes `[8, 32, 128, 512]` with ratio `0.2`.
- [x] 4.3 Add tests in `test_script/test_request_resource_contention_experiments.py` for average read-latency aggregation and baseline-relative normalization with baseline normalized to `1.0`.
- [x] 4.4 Add tests in `test_script/test_request_resource_contention_experiments.py` for grouped chart labels, colors, group names, and two-decimal data labels.
- [x] 4.5 Update existing read-impact CMT-hit validation tests in `test_script/test_request_resource_contention_experiments.py` to cover page-aligned page-read validation.

## 5. Verification

- [x] 5.1 Run `pytest test_script/test_request_resource_contention_experiments.py`.
- [x] 5.2 Run `python test_script/request_resource_contention_experiments.py --experiment read-impact` or a focused stubbed equivalent and confirm the generated read-impact results include baseline, four ratio-scan rows, four request-size-scan rows, and one grouped normalized-latency chart.
- [x] 5.3 Run `openspec validate refine-read-impact-contention-experiment --strict`.
