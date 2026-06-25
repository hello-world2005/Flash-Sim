## 1. GC/WL Maintenance Correctness

- [x] 1.1 Cover GC trigger boundaries and victim safety.
- [x] 1.2 Validate relocation through CMT, GMT, and fixed metadata fallback paths.
- [x] 1.3 Prevent stale GC writes from rewinding current mappings.
- [x] 1.4 Validate block/page accounting, barriers, erase finalization, and maintenance counters.
- [x] 1.5 Validate static WL candidate selection, capacity priority, non-recursion, and real media completion.

## 2. Backpressure And Cache Lifecycle

- [x] 2.1 Key waiting queues by complete physical plane and enforce FIFO retry.
- [x] 2.2 Preserve TSU multi-die batch scheduling across isolated queues.
- [x] 2.3 Make delayed cache flushes generation-aware and suppress duplicates.
- [x] 2.4 Rearm GC when reclaimed reserve capacity cannot release a first-write waiter.

## 3. Pressure Validation Tooling

- [x] 3.1 Add specialized low-invalid, overwrite-race, post-flush, and re-overwrite traces.
- [x] 3.2 Add timeout handling, structured per-trace summaries, and maintenance conservation checks.
- [x] 3.3 Distinguish correctness failures from valid write-coalescing warnings without treating the matrix as a workload-realism proof.

## 4. Verification

- [x] 4.1 Run the complete pytest suite (`270 passed`).
- [x] 4.2 Run the default 16-trace pressure matrix with zero request errors or residual queues.
- [x] 4.3 Validate all OpenSpec documents.
