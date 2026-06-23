## Why

The current read-impact resource-contention experiment is too narrow: it compares only a small fixed compute-contention setup and does not report normalized average read latency across the parameter scans needed to understand how inserted compute work affects otherwise identical CMT-hit read traffic. The experiment should keep the baseline and all experiment read streams identical while scanning compute insertion ratio and compute request size in a reproducible way.

## What Changes

- Change read-impact trace generation so the baseline and every experiment group contain the same CMT-initialized read requests with identical issue times, order, `start_lha`, and page-sized `read` request sizes.
- Replace the fixed read-impact contention case with two compute insertion scans:
  - insertion ratio scan with `ratio = num_compute_req / num_read_req` for `0.1`, `0.2`, `0.4`, and `0.8`, using compute size `128`;
  - compute request-size scan for sizes `8`, `32`, `128`, and `512`, using fixed insertion ratio `0.2`.
- Compute the average read latency for each baseline and experiment parameter condition.
- Emit machine-readable read-impact results that include raw average read latency and normalized latency relative to the baseline, whose normalized value is exactly `1.0`.
- Replace or extend the read-impact chart with one grouped bar chart containing baseline, ratio-scan, and request-size-scan groups on the same chart:
  - baseline bars use the default blue color;
  - ratio-scan bars use orange;
  - request-size-scan bars use purple;
  - bars within a group are closer together than bars across groups;
  - each group is labeled beneath the x-axis;
  - data labels show two decimal places.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `request-resource-contention-experiments`: Read-impact experiments must use identical CMT-hit read streams across baseline and experiment groups, scan compute insertion ratio and compute request size, report normalized average read latency, and render the requested grouped bar chart.

## Non-goals

- This change does not alter simulator scheduling, AMU mapping behavior, CMT replacement, NAND timing, or compute/search execution semantics.
- This change does not change the isolated compute/search size-scan experiment behavior.
- This change does not introduce a general-purpose workload generator outside the existing resource-contention experiment script.
- This change does not change request latency JSON/CSV report schemas beyond consuming existing per-request timing fields.

## Impact

- In scope modules and functions:
  - `test_script/request_resource_contention_experiments.py`: read-impact trace generation, compute insertion helpers, experiment sweep orchestration, average read-latency aggregation, CSV/JSON output, and plotting.
  - `test_script/test_request_resource_contention_experiments.py`: regression coverage for identical read portions, ratio and size scan configurations, average read-latency aggregation, and chart data/layout inputs.
  - `output/request_resource_contention_experiments/results/*`: generated read-impact results and grouped chart artifacts may change when the experiment is rerun.
- Out of scope modules and functions:
  - Core simulator modules under `flash_sim/`, including AMU/FTL/PHY scheduling and latency report generation.
  - Existing request latency report tests unless a failing assertion needs a fixture update due to regenerated experiment outputs.
- Test targets:
  - Verify baseline and experiment traces contain the same CMT-initialized page-sized reads with identical issue time, order, `start_lha`, and size.
  - Verify ratio scan conditions are `[0.1, 0.2, 0.4, 0.8]` with compute size `128`, and size scan conditions are `[8, 32, 128, 512]` with ratio `0.2`.
  - Verify aggregation computes average read latency only from `READ` report entries and normalizes each experiment value by the baseline average, with baseline normalized to `1.0`.
  - Verify the grouped chart receives three x-axis groups, the requested colors, wider gaps between groups than within groups, group labels, and two-decimal data labels.
