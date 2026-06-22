## 1. Experiment Tooling Skeleton

- [x] 1.1 Add `test_script/request_resource_contention_experiments.py` with default output-root constants, CLI argument parsing, and importable entry functions; do not delete or modify existing simulator runtime modules.
- [x] 1.2 Add size validation and static-address helper functions in `test_script/request_resource_contention_experiments.py` for legal `compute` and `search` engine trace commands.
- [x] 1.3 Add trace-writing helpers in `test_script/request_resource_contention_experiments.py` for single-request traces and deterministic experiment filenames under `output/request_resource_contention_experiments/`.
- [x] 1.4 Add an engine-run helper in `test_script/request_resource_contention_experiments.py` that calls `flash_sim.engine.Engine.Start_simulation(...)`, suppresses noisy stdout/stderr for scripted runs, and loads the JSON report from `engine.last_request_latency_report_path`.

## 2. Size-Scan Experiment

- [x] 2.1 Add `run_size_scan(...)` in `test_script/request_resource_contention_experiments.py` to generate one trace and invoke one simulator run for every `(request_type, size)` pair.
- [x] 2.2 Add aggregation helpers in `test_script/request_resource_contention_experiments.py` that preserve raw `host_completion_time` and `total_latency` for each `compute` and `search` result.
- [x] 2.3 Add `normalize_size_scan_results(...)` in `test_script/request_resource_contention_experiments.py` using per-request-type max-latency normalization and zero-max handling.
- [x] 2.4 Add aggregate JSON/CSV writers in `test_script/request_resource_contention_experiments.py` for raw and normalized size-scan data.
- [x] 2.5 Add dependency-free SVG bar-chart writer functions in `test_script/request_resource_contention_experiments.py` and write separate `compute` and `search` normalized latency charts.

## 3. Read-Impact Experiment

- [x] 3.1 Add read-request selection helpers in `test_script/request_resource_contention_experiments.py` that derive valid default `read` commands from `pre_data/precondition_data.json`.
- [x] 3.2 Add paired-trace generation in `test_script/request_resource_contention_experiments.py` for a baseline read trace and a compute-contention trace whose read portions are identical.
- [x] 3.3 Add exactly two prepended `compute` commands to the compute-contention trace and keep the remaining commands byte-for-byte identical to the baseline read trace.
- [x] 3.4 Add `run_read_impact_comparison(...)` in `test_script/request_resource_contention_experiments.py` to simulate both traces, validate both prepended compute completions occur after the first read issue time, and compare reads by `(type, time, start_lha, size)`.
- [x] 3.5 Add read-impact JSON/CSV output writers in `test_script/request_resource_contention_experiments.py` that report baseline completion time, contended completion time, and contended-minus-baseline delta for each matched read.

## 4. CLI Integration

- [x] 4.1 Wire `main(argv)` in `test_script/request_resource_contention_experiments.py` so users can run the size-scan experiment, the read-impact experiment, or both with configurable sizes and output root.
- [x] 4.2 Ensure CLI output prints the generated trace paths, aggregate result paths, chart paths, and read-impact comparison paths without changing existing `flash-sim` console scripts.

## 5. Verification

- [x] 5.1 Add `test_script/test_request_resource_contention_experiments.py` tests for size validation, static single-request trace generation, and one-trace-per-size-per-request-type planning.
- [x] 5.2 Add tests in `test_script/test_request_resource_contention_experiments.py` for normalization behavior, zero-max handling, aggregate output shape, and SVG chart labels.
- [x] 5.3 Add tests in `test_script/test_request_resource_contention_experiments.py` for paired read trace identity, two-command compute prefix placement, and explicit mismatch failure.
- [x] 5.4 Add a focused end-to-end or stubbed-engine test in `test_script/test_request_resource_contention_experiments.py` proving report aggregation reads `host_completion_time` and `total_latency` from generated request latency JSON.
- [x] 5.5 Run `pytest test_script/test_request_resource_contention_experiments.py` and any directly affected existing latency-report tests.
