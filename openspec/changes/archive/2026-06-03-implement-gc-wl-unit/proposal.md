## Why

The current `GC_WL_Manager` only reclaims invalid pages when a plane runs low on free blocks, so erase counts can drift without any balancing policy. We need a single GC/WL unit that keeps the existing event-driven GC flow but also adds MQSim-style dynamic and static wear leveling before the write-path and victim-selection logic diverge further.

## What Changes

- Rename the current GC controller class from `GC_WL_Manager` to `GC_WL_Unit` and keep it as the single owner of garbage collection and wear-leveling decisions.
- Add dynamic wear leveling to free-block allocation so normal writes prefer the least-erased eligible block instead of consuming free blocks in implicit block-index order.
- Add static wear leveling that can migrate cold valid pages out of the least-erased safe block after GC erase completion, using the same transaction-chain model as GC.
- Extend victim/destination selection so the unit can distinguish GC victims, dynamic WL allocation targets, and static WL migration targets with explicit “safe block” rules.
- Reuse and extend existing LPA/MVPN barrier handling so GC/static-WL migrations lock affected mappings until the relocation chain finishes.
- Add regression coverage for GC victim selection, dynamic WL free-block reuse, and static WL-triggered cold-block migration.
- **BREAKING**: direct imports or type references to `flash_sim.FTL.GC_WL_Manager` will need to move to `GC_WL_Unit`.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `ftl-scheduling-and-media-model`: GC behavior now includes dynamic/static wear leveling, safe-block eligibility, and a renamed GC/WL control unit.

## Impact

- In scope modules/functions:
  - `flash_sim/FTL.py`: `GC_WL_Manager` rename, GC/WL policy state, victim selection, migration flow, free-block allocation hooks, and FTL wiring.
  - `flash_sim/FTL.py`: `Block_Manager.get_write_frontier(...)`, `finalize_gc_erase(...)`, and any helper state needed to track free-block ordering and WL follow-up triggers.
  - `flash_sim/FTL.py`: AMU / TSU interaction points that currently enforce GC-related barriers.
  - `tests/`: new or updated GC/WL unit tests covering the changed allocation and migration behavior.
- Out of scope modules/functions:
  - `flash_sim/PHY.py` timing model changes beyond what is required to execute the existing transaction chains.
  - `flash_sim/HIL.py`, `flash_sim/engine.py`, or CLI trace formats unrelated to GC/WL behavior.
  - Replacing the simulator’s existing transaction types or redesigning the host/device request pipeline.
- Proposed test targets:
  - A plane with multiple free blocks at different erase counts should allocate the lowest-erase free block when dynamic WL is enabled.
  - After a GC erase returns a block to the pool, a static WL eligibility check should be able to trigger a cold-block migration when wear skew exceeds the configured threshold.

## Non-goals

- Reproducing every MQSim GC heuristic and policy flag in this change.
- Introducing a new standalone capability outside the existing FTL scheduling/media model spec.
- Reworking unrelated cache-pressure scheduling, PCIe behavior, or NAND timing semantics.
