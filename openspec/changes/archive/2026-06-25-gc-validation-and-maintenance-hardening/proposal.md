## Why

The GC and wear-leveling paths are core simulator behavior, but their correctness depends on several asynchronous invariants spanning AMU mapping state, block bookkeeping, waiting queues, cache flush lifecycle, TSU scheduling, PHY completion, and request reporting. This change hardens those invariants and promotes the pressure-validation workflow into a maintained simulator tool.

## What Changes

- Strengthen GC trigger, victim selection, relocation, mapping, bookkeeping, barrier, overwrite, waiting queue, and static-WL behavior.
- Scope waiting queues to the complete physical plane and preserve strict FIFO without reducing TSU multi-die batch parallelism.
- Make retained cache flushes generation-aware and rearm GC when a reserved block cannot release a first-write waiter.
- Add focused unit/runtime regressions and a real Engine/TSU/PHY static-WL end-to-end test.
- Expand the pressure matrix with specialized traces, per-trace timeouts, maintenance counters, conservation checks, and warning/error separation.

## Capabilities

### Modified Capabilities

- `ftl-scheduling-and-media-model`: GC/WL correctness, physical-plane waiting queues, cache-flush retry semantics, and maintenance forward progress.
- `simulator-tooling`: complete GC pressure matrix coverage, bounded execution, structured results, and static-WL event-path validation.

## Impact

- Runtime: `flash_sim/FTL.py`, `flash_sim/HIL.py`, `flash_sim/Device.py`, `flash_sim/common.py`, and reporting/configuration helpers.
- Tests: focused GC/WL unit and runtime tests, data-cache regressions, matrix validation, and specialized pressure traces.
- Tooling: GC pressure trace generation and matrix execution.

## Non-goals

- Introduce metadata out-of-place update or metadata GC.
- Model incremental die dispatch while a chip is already busy.
- Remove the documented PHY/GC realism simplifications.
- Preserve temporary debugging notes or session-local checklists as OpenSpec capabilities.
