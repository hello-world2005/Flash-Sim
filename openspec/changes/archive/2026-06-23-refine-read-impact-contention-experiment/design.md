## Context

`test_script/request_resource_contention_experiments.py` already supports isolated compute/search size scans and a read-impact comparison that builds identical baseline and contended read portions. The current read-impact path is still a fixed comparison: the contended trace prepends two compute requests, then writes per-read completion deltas. That is not enough to evaluate how read latency changes as compute insertion ratio or compute request size changes.

The existing CMT-hit validation should remain the foundation. The revised experiment should generate a stable CMT-hit read stream once, reuse it unchanged for the baseline and every experiment trace, and then vary only the inserted compute requests.

## Goals / Non-Goals

**Goals:**

- Generate page-aligned, page-sized read requests from the CMT-hit precondition set so every measured read targets exactly one page.
- Keep read issue time, order, `start_lha`, and `size` identical across the baseline, ratio scan traces, and compute-size scan traces.
- Run a baseline with no inserted compute requests, a ratio scan for `[0.1, 0.2, 0.4, 0.8]` using compute size `128`, and a compute-size scan for `[8, 32, 128, 512]` using ratio `0.2`.
- Aggregate average read latency from request latency JSON report entries whose `type` is `READ`.
- Normalize every average read latency by the baseline average and force the baseline normalized latency to `1.0`.
- Write one grouped SVG bar chart with baseline, ratio scan, and compute-size scan groups, including the requested colors, group spacing, group labels, and two-decimal value labels.

**Non-Goals:**

- Do not change simulator scheduling, CMT replacement, mapping resolution, NAND timing, or request latency report schemas.
- Do not change the existing isolated compute/search size-scan workflow.
- Do not add warm-up trace commands; CMT-hit setup should remain a simulator precondition input.
- Do not make the charting layer depend on a new plotting library unless the existing manual SVG output cannot satisfy the layout.

## Decisions

1. Build the read stream from the CMT-hit precondition set.

   The default read stream should choose valid precondition records up to the read-impact CMT warm capacity, write those records as the read-impact precondition file, and generate one read command per selected LPA. Each read command should be page-aligned with `start_lha = lpa * SECTOR_PER_PAGE` and `size = SECTOR_PER_PAGE`, so each request targets one page. Custom read commands should still pass through the existing CMT-hit validation and should be rejected if they are not page-aligned page reads.

   Alternative considered: keep the small default `read_count` behavior. That keeps tests cheap but conflicts with the requested use of all CMT-initialized cached reads and makes ratio scans less representative.

2. Insert compute commands deterministically without mutating reads.

   For each experiment condition, compute `num_compute_req = max(1, round(num_read_req * ratio))`. Choose insertion anchors evenly across the read index range and insert each compute command immediately before its anchor read with the same issue time. The inserted compute commands should use static-region addresses via the existing static request helpers and increasing slots to avoid accidental address aliasing. The filtered read command list in every generated trace must still exactly match the baseline read list.

   Alternative considered: prepend all compute commands before the first read. That preserves read fields but does not represent the requested insertion-ratio sweep as well and overweights early-read interference.

3. Treat the baseline as a separate no-compute condition.

   The baseline should run once and produce `average_read_latency` and `normalized_latency = 1.0`. Ratio and size scan rows should record their group name, parameter label/value, compute ratio, compute size, compute count, raw average read latency, and normalized latency. The experiment can still keep per-read comparison details internally or in optional output, but the primary read-impact result should be condition-level average latency.

   Alternative considered: normalize each scan by its maximum, like the existing size-scan experiment. That would obscure the requested comparison against the control group.

4. Render the grouped chart with the existing SVG approach.

   A dedicated read-impact chart writer should lay out three x-axis groups: baseline, ratio scan, and request-size scan. Baseline uses the default blue fill, ratio scan uses orange, and size scan uses purple. Within each group, bars use a smaller gap; between groups, the layout reserves a larger gap. Parameter labels belong under each bar, and group labels belong below their corresponding group.

   Alternative considered: use Matplotlib. The repository already emits SVG charts without a plotting dependency, and the requested layout is straightforward to build with deterministic SVG coordinates.

## Design Rationale

The key design constraint is isolating compute-resource contention from read-workload drift. By deriving one CMT-hit page-read stream and reusing it verbatim for every condition, any measured read-latency change comes from the additional compute commands rather than different read addresses, sizes, issue times, or mapping paths. The condition-level output also matches the experiment question more directly than the current per-read completion-delta table: each scanned parameter produces one average read latency and one normalized bar.

## Risks / Trade-offs

- [Risk] `round(num_read_req * ratio)` can produce an effective ratio that is close to but not exactly the configured ratio for some read counts. -> Mitigation: record both configured ratio and actual compute count in output so the experiment remains auditable.
- [Risk] Running all CMT-warmed reads for every condition increases simulation time. -> Mitigation: keep `read_count` or custom read commands available as explicit test hooks while default experiment behavior uses the full CMT-hit set.
- [Risk] Same-time compute/read insertion depends on trace-order handling for tie-breaking. -> Mitigation: insert compute commands immediately before anchor reads and add tests that assert the generated command order.
- [Risk] Manual SVG positioning can regress visually. -> Mitigation: test for group labels, color fills, two-decimal labels, and the presence of larger group gaps through deterministic chart metadata or SVG text.

## Migration Plan

1. Add constants for read-impact ratio scan values, compute-size scan values, fixed ratio `0.2`, and ratio-scan compute size `128`.
2. Update default read command generation to produce page-aligned page reads from the selected CMT-hit precondition set.
3. Add helpers to calculate compute counts, choose deterministic insertion anchors, and build condition-specific compute-inserted traces.
4. Replace the fixed two-compute read-impact run with baseline, ratio scan, and size scan execution.
5. Add aggregation helpers for average read latency, baseline normalization, JSON/CSV result output, and grouped SVG chart output.
6. Update CLI summary and optional arguments to report the new chart and result files.
7. Update focused tests in `test_script/test_request_resource_contention_experiments.py`.
8. Rollback is limited to restoring the previous fixed read-impact comparison path; core simulator behavior remains unchanged.

## Open Questions

- Should the final implementation keep the previous per-read completion-delta CSV as an auxiliary artifact for debugging, or replace it entirely with the new condition-level CSV? The primary required output is the normalized average-latency scan.
