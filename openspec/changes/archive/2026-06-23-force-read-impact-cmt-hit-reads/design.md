## Context

`test_script/request_resource_contention_experiments.py` builds read-impact traces by selecting valid records from `pre_data/precondition_data.json`, then simulates a baseline read trace and a compute-contention trace. The read portions are identical, but the simulator preconditioning step warms only a subset of user LPAs into CMT using `Block_Manager.preconditioning(...)`. Because that warm subset is randomized, two simulator runs can send otherwise identical reads through different AMU paths: one can hit CMT while the other triggers `mapping_read`.

The request latency recorder already tracks mapping resolution counts internally, but the JSON report does not currently expose those counts to the experiment workflow. The experiment therefore needs both a deterministic CMT-hit setup and a report-level assertion that no compared read used the mapping-read path.

## Goals / Non-Goals

**Goals:**

- Ensure read-impact baseline and contended simulations use the same preconditioned user data and the same CMT-hit-only read path.
- Generate or validate read commands so every touched LPA is represented by a valid precondition record and can be warmed into CMT.
- Pass the same generated CMT-hit precondition file into both simulator runs.
- Expose mapping-resolution counts in request latency JSON and validate that every compared read has only CMT-hit resolutions.
- Keep the existing paired-trace read identity and comparison output semantics.

**Non-Goals:**

- Do not change normal AMU translation, CMT eviction, GMT/GTD, or mapping-page read behavior.
- Do not change ONFI channel arbitration, compute scheduling, or PHY timing.
- Do not make CMT globally deterministic for every simulator use case.
- Do not include warm-up read commands in the measured read-impact traces.

## Decisions

1. Build a minimal read-impact precondition file from the selected read commands.

   The experiment should derive the set of all LPAs touched by the measured read commands, copy only those matching records from the source precondition data, and write that subset under the experiment output root. Both baseline and contended simulations should pass this generated file as `pre_trace`.

   Rationale: if the precondition file contains no unrelated user LPAs and the touched LPA count is no greater than the runtime CMT warm capacity, the existing preconditioning step will warm every measured read LPA regardless of shuffle order. This keeps the fix local to experiment setup.

   Alternative considered: seed `random` before each simulation. This would make the warm subset repeatable, but it still would not guarantee the selected read LPAs are in that subset.

2. Validate touched-LPA coverage before running the simulator.

   The experiment should compute touched LPAs and sector offsets from each read command's `start_lha` and `size`, then verify every touched sector is valid in the selected precondition records. It should also reject read sets whose touched LPA count exceeds the CMT warm capacity.

   Rationale: this fails early with a clear experiment error instead of letting one run fall into `mapping_read` or invalid data access later.

   Alternative considered: rely only on post-simulation report validation. That catches the bad result but wastes time and makes the generated traces less self-describing.

3. Expose `mapping_resolution_counts` in request latency JSON.

   Add a backward-compatible per-request field that mirrors the recorder's existing counters: `cmt_hit`, `gmt_hit`, `mapping_read`, and `uncached_write`. The read-impact workflow should use this field to assert `mapping_read == 0` and `cmt_hit == total_mapping_lookups` for every compared read in both reports.

   Rationale: direct counters are less brittle than inferring mapping behavior from CSV text or timing intervals.

   Alternative considered: parse the generated CSV `Cache Hit` column. That loses the distinction between no mapping lookup and a real all-CMT-hit read, and it is less convenient for the JSON-based experiment pipeline.

4. Keep comparison rows focused on measured reads.

   The generated precondition file should be an input to simulator initialization, not a trace prefix. No warm-up reads should be added to either trace, so the existing comparison by read identity remains unchanged.

   Rationale: adding warm-up requests would require new filtering rules and could introduce extra channel contention before the measured workload.

## Design Rationale

The least invasive way to make the experiment fair is to control the experiment's precondition input, not the global simulator policy. A minimal precondition file lets the existing runtime initialize real user pages, mapping pages, GTD entries, and CMT entries through the normal code path, while eliminating unrelated LPAs that would otherwise compete for randomized CMT warm-up slots. The report assertion then protects against future changes to preconditioning or read segmentation that would accidentally reintroduce `mapping_read` into the comparison.

## Risks / Trade-offs

- [Risk] A custom read command can span multiple LPAs and exceed the CMT warm capacity. -> Mitigation: validate all touched LPAs before simulation and fail with a clear error.
- [Risk] Adding a JSON report field can affect snapshot-style tests. -> Mitigation: make the field additive and update focused report tests to assert its presence without changing existing field names.
- [Risk] The minimal precondition file may not preserve unrelated precondition data some future experiment expects. -> Mitigation: apply it only to `run_read_impact_comparison(...)`; size-scan and other workflows keep their current preconditioning behavior.
- [Risk] CMT warm capacity is currently implicit through `CMT_SIZE` and the preconditioning ratio default. -> Mitigation: calculate the same capacity the runtime uses and keep validation close to the helper that writes the read-impact precondition file.

## Migration Plan

1. Add helper functions for touched-LPA extraction, precondition-record lookup, read-sector validity validation, CMT warm-capacity calculation, and minimal precondition file writing.
2. Wire `run_read_impact_comparison(...)` to generate the CMT-hit precondition file and pass it as `pre_trace` to both simulator runs.
3. Add `mapping_resolution_counts` to request latency JSON export.
4. Add report validation for baseline and contended read reports before computing comparison rows.
5. Update focused experiment and latency-report tests.
6. Rollback is removing the new helpers, validation calls, generated precondition file path, and additive JSON field; core simulator behavior remains unchanged.

## Open Questions

- Should a future CLI option allow users to bypass CMT-hit enforcement for intentionally studying mapping-read contention? This change should keep enforcement on for read-impact by default because the current experiment is meant to isolate compute channel-resource impact.
