## Context

The event-driven simulator already accepts engine traces with `type`, `time`, `start_lha`, and `size`, and `Engine.Start_simulation(trace_path)` already writes per-request JSON/CSV latency reports under `report/`. Existing test tooling lives under `test_script/`, while generated logs and visualization artifacts commonly live under `output/`.

This change adds a repeatable experiment layer on top of those existing behaviors. It should generate traces, run the simulator, read the generated request latency report, and write experiment-specific aggregate outputs without changing the simulator timing model or request report schema.

## Goals / Non-Goals

**Goals:**

- Provide a callable and CLI-friendly experiment workflow for isolated `compute` and `search` size scans.
- Generate one single-request trace per `(request_type, size)` and run the event-driven simulator once for each generated trace.
- Preserve raw latency data and also produce normalized latency values for plotting.
- Produce a bar chart with `size` on the x-axis and normalized latency on the y-axis.
- Generate paired baseline/contention read traces where read entries are identical and the contention trace prepends two `compute` requests.
- Compare read completion times between the paired traces.
- Keep the implementation deterministic and testable through pure helper functions plus small end-to-end coverage.

**Non-Goals:**

- Do not change `Engine`, `RequestLatencyRecorder`, PHY timing, TSU scheduling, AMU mapping, cache behavior, or PCIe semantics unless a blocking bug is discovered during implementation.
- Do not redefine the existing request latency JSON or CSV schema.
- Do not create a general workload-generation framework beyond these two experiments.
- Do not require external plotting dependencies for the default workflow.

## Decisions

1. Add an experiment module/script under `test_script/` with importable helper functions and a `main(argv)` CLI.

   Rationale: the repository already keeps trace-generation tooling and regression tests under `test_script/`, and pytest only discovers `test_*.py`, so a non-test experiment script can live there without being collected as a test. Keeping helpers importable lets tests validate trace generation, aggregation, normalization, and chart output without shelling out.

   Alternative considered: add a package module under `flash_sim/experiments/`. That is cleaner for reusable APIs, but this request is experiment tooling rather than simulator runtime functionality.

2. Use `output/request_resource_contention_experiments/` as the default experiment output root.

   Rationale: generated traces, aggregate results, and charts are run outputs rather than stable fixtures. The script should still accept `--output-root` for users who want a clean experiment directory elsewhere.

   Alternative considered: write traces under `test_case/`. That would blur generated experiment data with committed regression fixtures.

3. Use dependency-free SVG bar charts by default.

   Rationale: `pyproject.toml` currently has no runtime dependencies. A simple bar chart can be generated as SVG with deterministic coordinates, making tests easy and avoiding a heavy plotting dependency. The aggregate JSON/CSV remains the source of truth for downstream plotting in other tools.

   Alternative considered: add `matplotlib`. It is familiar, but it expands install cost for a narrow visualization need.

4. Normalize isolated size-scan latencies per request type.

   Rationale: the user asked to test `compute` and `search` separately. Normalizing each request family by that family's maximum measured latency keeps each chart focused on how latency scales with `size`. Raw `total_latency` and `host_completion_time` values remain in the aggregate output.

   Alternative considered: normalize all `compute` and `search` results by one global maximum. That makes cross-family comparisons easier but makes the separate charts less readable if one family dominates the scale.

5. Compare read-impact traces by read request identity rather than by trace index alone.

   Rationale: the contention trace prepends two `compute` requests, so matching by raw trace index would be off by two. Each read comparison should match `(type, time, start_lha, size)` and report baseline completion, contended completion, and delta.

   Alternative considered: compare reads by order after filtering to `READ`. That is simpler, but matching identities catches accidental read trace drift.

## Design Rationale

The experiments should sit above the simulator, not inside it. This keeps the measured behavior faithful to the current event-driven runtime and avoids adding experiment-only branches to scheduling or reporting code. Trace construction is deterministic, simulation invocation remains the same public `Engine.Start_simulation(...)` path used by existing e2e tests, and all comparisons are derived from the existing request latency JSON report.

The SVG chart choice is intentionally plain: it provides a real visual artifact while preserving a low-friction Python environment. Users who need publication-quality charts can replot from the aggregate JSON/CSV, while the repository still satisfies the experiment requirement out of the box.

## Risks / Trade-offs

- [Risk] Generated read requests may target data that is not valid in the current preconditioned state. -> Mitigation: derive default read requests from `pre_data/precondition_data.json` records with valid sectors, and allow explicit read trace parameters only after validating schema consistency.
- [Risk] The two prepended `compute` requests might finish before the first read issue time if timing constants or default sizes change drastically. -> Mitigation: validate the simulated report after running the contention trace and fail with a clear error unless both prepended compute requests complete after the first read issue time.
- [Risk] SVG charts are less flexible than a plotting library. -> Mitigation: write raw and normalized results to machine-readable aggregate files so external plotting remains straightforward.
- [Risk] Running one simulator instance per size can be slow for large size lists. -> Mitigation: keep the size list configurable and make the default list small enough for routine use.
- [Risk] Existing report files under `report/` may be overwritten by identical trace names. -> Mitigation: generate unique trace names under the experiment output root and load the report path returned by each `Engine` instance immediately after each run.

## Migration Plan

1. Add the experiment script/helpers and tests.
2. Document the CLI usage in the script help text or README if implementation scope allows.
3. No data migration is required.
4. Rollback is removing the new experiment script, generated-output conventions, tests, and optional documentation. Core simulator behavior remains unchanged.

## Open Questions

- Whether future experiments should also provide a search-prefixed read-impact variant. This change requires the compute-prefixed pair described by the request and keeps search-specific impact as future scope unless added explicitly during implementation.
