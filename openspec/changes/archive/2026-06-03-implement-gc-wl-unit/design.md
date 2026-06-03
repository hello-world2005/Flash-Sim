## Context

The current event-driven FTL keeps all GC behavior inside `flash_sim/FTL.py::GC_WL_Manager`. Today that class only reacts to low free-block pressure, picks a victim block by maximum invalid-page count, migrates valid pages with `GC_READ -> GC_WRITE -> GC_ERASE`, and returns the erased block to `free_block_pool`. It does not influence which free block gets allocated next, and it does not perform any cold-data relocation after erase completion, so wear counts can diverge indefinitely.

This change extends an already cross-cutting path:
- `Block_Manager` owns per-plane free/valid/invalid bookkeeping and write-frontier movement.
- `GC_WL_Manager` decides when to reclaim or relocate blocks.
- `TSU` and `Address_Mapping_Unit` already provide the barrier semantics used by GC migration.

Because dynamic WL changes free-block allocation and static WL reuses the GC transaction chain, the design has to coordinate state across block selection, transaction submission, and barrier release instead of adding an isolated helper.

## Goals / Non-Goals

**Goals:**
- Rename the GC controller to `GC_WL_Unit` so the code reflects that it owns both garbage collection and wear leveling.
- Add dynamic wear leveling to free-block allocation for user writes, mapping writes, and GC destination allocation.
- Add static wear leveling as a post-erase follow-up that can migrate cold valid pages out of the least-erased safe block when wear skew warrants it.
- Reuse the current transaction-chain and barrier model so GC/WL relocations remain visible to TSU, PHY, and AMU without inventing a second migration pipeline.
- Keep the policy testable with deterministic unit tests around block choice and WL-trigger submission.

**Non-Goals:**
- Reimplement every MQSim victim-selection policy (`RGA`, `RANDOM_P`, `FIFO`, etc.) in this change.
- Redesign the host/device request pipeline or introduce a new persistence/completion model for writes.
- Add new public CLI trace knobs or external configuration files for GC/WL policy tuning.

## Decisions

### 1. Keep a single GC/WL control unit and rename it to `GC_WL_Unit`

The current simulator already centralizes reclaim decisions in one class. We will preserve that ownership boundary and rename the class instead of splitting GC and WL into separate coordinators.

Rationale:
- GC and static WL both submit relocation chains and need the same AMU/TSU/PHY contracts.
- A single owner can evaluate wear state immediately after erase completion without extra callbacks or duplicated bookkeeping.

Alternatives considered:
- Separate `GC_Unit` and `WL_Unit`: rejected because it would require another synchronization layer around block safety, free-pool updates, and migration barriers.
- Keep the old class name: rejected because the implementation would no longer match the semantics.

### 2. Make free-block selection WL-aware at the `Block_Manager` boundary

`Block_Manager.get_write_frontier(...)` is where the runtime currently advances to the next block when a frontier block fills. Dynamic WL will hook at this boundary so the next frontier block is chosen from the plane’s eligible free blocks by lowest erase count instead of implicit block order.

Planned shape:
- Keep `free_block_pool` as the source-of-truth membership set.
- Add a helper owned by `GC_WL_Unit` or `Block_Manager` that scans or indexes eligible free blocks by `wl_level`.
- Use that helper whenever a new user/mapping frontier block or GC destination block must be chosen.

Rationale:
- This keeps wear-leveling decisions attached to allocation points instead of running a separate background pass.
- The simulator’s current geometry sizes are small enough that an explicit selection helper is acceptable even if it scans the pool.

Alternatives considered:
- Maintain a Python equivalent of MQSim’s multimap keyed by erase count: viable, but more invasive; we can still expose helpers that make a later ordered structure drop-in compatible.
- Continue using numeric block order and only add static WL: rejected because it would leave the main write path wear-oblivious.

### 3. Preserve the current GC victim heuristic, but extend the unit with explicit safe-block eligibility

This change keeps the existing “prefer the block with the most invalid pages” GC victim rule as the baseline reclaim heuristic. The new behavior is to apply explicit eligibility filtering before a block can serve as:
- a GC victim,
- a static-WL source,
- or a static-WL / GC relocation destination.

Safe-block filtering will exclude blocks that are:
- the current write frontier for active user or mapping allocation,
- already protected by a GC/WL erase barrier,
- or carrying in-flight program ownership that would make relocation unsafe.

Rationale:
- The prompt asks for wear leveling, not a full policy-matrix port.
- Explicit eligibility rules are the behavior change that protects correctness when static WL begins moving cold data.

Alternatives considered:
- Port the full MQSim victim-policy menu immediately: rejected as too large for one change.
- Reuse existing barriers without adding eligibility helpers: rejected because static WL needs to distinguish cold safe blocks from merely low-wear blocks.

### 4. Trigger static WL after erase completion and execute it with the existing migration chain

After `finalize_gc_erase(...)` returns a block to the free pool, `GC_WL_Unit` will evaluate whether the plane’s wear skew exceeds a static-WL threshold. If it does, the unit selects the least-erased safe block as the cold-data source, picks an eligible higher-wear free destination block, and submits the same `GC_READ -> GC_WRITE -> GC_ERASE` pattern used for GC relocation.

Rationale:
- This mirrors the MQSim trigger point described in the prompt while fitting the simulator’s current callback structure.
- Reusing the same transaction types and flow keeps PHY, TSU, and AMU integration small.

Alternatives considered:
- Run static WL on a periodic timer: rejected because the simulator has no independent background clock source for maintenance today.
- Add dedicated `WL_READ` / `WL_WRITE` / `WL_ERASE` transaction types now: rejected because the behavior can be expressed with the existing chain and barrier logic.

### 5. Extend the barrier model instead of inventing a second relocation lock path

GC migrations already protect LPAs with the block-manager and AMU barrier books until relocation completes. Static WL will reuse that mechanism:
- lock the LPAs (and mapping writes if needed) for valid pages being relocated,
- prevent TSU from dispatching conflicting user or mapping work,
- and release barriers when the corresponding relocation write or erase completes.

Rationale:
- The correctness story is already established in the existing request/transaction pipeline.
- Sharing one barrier model makes GC and static WL observable to the same tests.

Alternatives considered:
- Introduce a second WL-only barrier table: rejected because it would duplicate conflict checks and create inconsistent scheduler behavior.

## Design Rationale

The main design choice is to treat wear leveling as a first-class extension of the existing GC unit rather than as a background service. Dynamic WL belongs at allocation time because that is where erase-count balancing has direct leverage over future wear. Static WL belongs immediately after erase completion because that is the one point where the simulator already reclassifies a block as free and can safely assess whether wear skew is widening. Reusing the current transaction-chain and barrier model keeps the implementation aligned with the simulator’s event-driven architecture instead of introducing a second maintenance pipeline with slightly different semantics.

## Risks / Trade-offs

- [Dynamic WL adds extra free-block scans on every frontier transition] -> Keep the first implementation simple and deterministic; optimize the free-pool index later only if profiling shows pressure.
- [Static WL can amplify write traffic and slow steady-state tests] -> Gate static WL behind an explicit wear-skew threshold and only evaluate it after erase completion.
- [Renaming `GC_WL_Manager` breaks direct imports in tests or scripts] -> Update internal references in one change and call out the rename as a breaking API adjustment in the proposal/tasks.
- [Barrier release bugs could deadlock unrelated requests] -> Reuse the existing GC barrier-release path and add regression tests that assert blocked work resumes after relocation completes.

## Migration Plan

1. Rename `GC_WL_Manager` references to `GC_WL_Unit` in `FTL`, block-manager wiring, and any direct tests/imports.
2. Introduce WL-aware block-selection helpers and route frontier / GC-destination allocation through them.
3. Add static-WL trigger evaluation after erase completion and reuse the existing relocation chain submission flow.
4. Extend barrier and safe-block checks so static WL and GC share one protection model.
5. Add or update tests for dynamic WL allocation choice, GC victim safety filtering, and static WL trigger submission.

Rollback strategy:
- Reverting this change cleanly means restoring the old class name, the previous frontier-selection logic, and the prior GC-only post-erase path.

## Open Questions

- Should the initial static-WL wear-skew threshold be a constant in `FTL.py` or a field on an existing config object for easier scenario tuning?
- Do we want to expose GC victim-policy selection in the same change, or keep the current greedy invalid-page-max heuristic as the only supported policy for now?
- Is it sufficient to model static WL with existing `GC_*` transaction types, or do downstream visualizers/tests need an explicit way to distinguish WL relocations from reclaim relocations later?
