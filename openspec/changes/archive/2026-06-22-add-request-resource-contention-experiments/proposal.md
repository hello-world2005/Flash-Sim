## Why

The simulator can report per-request latency, but there is no repeatable experiment workflow for isolating `compute`/`search` request latency by size or for comparing how their channel-resource occupancy affects otherwise identical `read` traffic. This change adds reproducible experiments so performance effects can be measured, plotted, and regression-tested instead of inspected manually.

## What Changes

- Add an experiment workflow that scans configured request sizes for `compute` and `search`, generates one single-request trace per `(request_type, size)`, runs the event-driven simulator once per trace, collects completion latency, normalizes the latency values, and writes a bar chart with `size` on the x-axis and normalized latency on the y-axis.
- Add a paired-trace experiment for read-impact analysis: both traces contain identical `read` requests with identical issue times, and one trace prepends two `compute` requests whose completion times extend beyond the first `read` issue time.
- Collect simulator outputs for both read-impact traces and compare `read` completion times between the baseline trace and the compute-prefixed trace.
- Add automated tests for trace shape, size-scan result aggregation, normalization behavior, and read-trace identity constraints.
- No breaking changes to existing simulator trace parsing or request latency report formats.

## Capabilities

### New Capabilities

- `request-resource-contention-experiments`: Defines reproducible experiment tooling for isolated `compute`/`search` latency scans, normalized bar-chart generation, and paired read-impact comparison traces.

### Modified Capabilities

- None.

## Non-goals

- This change does not alter core NAND timing, PCIe timing, TSU scheduling, AMU mapping, cache, or PHY execution semantics.
- This change does not redefine the request latency JSON/CSV report schema.
- This change does not add a general-purpose workload generator beyond the traces needed by these experiments.
- This change does not require changing default simulator configuration values.

## Impact

- In scope: new experiment script/module under the repository tooling area, generated experiment trace files under a deterministic output directory, result aggregation from existing request latency JSON reports, normalized chart generation, and tests under `test_script/`.
- In scope functions/modules likely touched or consumed: `flash_sim.engine.Engine.Start_simulation`, `flash_sim.request_latency_report`, existing event-driven trace schema handling, and any small local helpers needed to run simulations and parse reports.
- Out of scope: changing `flash_sim.config`, `flash_sim.common`, `flash_sim.PHY`, `flash_sim.PCIe_link`, request scheduling algorithms, and report field names unless implementation reveals a blocking bug.
- Dependencies: plotting support may use an existing project dependency if present, otherwise add a lightweight dependency such as `matplotlib` only if needed.
- Test target: add tests that validate the generated single-request traces and paired read-impact traces, then verify result aggregation/normalization can be exercised with deterministic sample reports or a small end-to-end simulator run.
