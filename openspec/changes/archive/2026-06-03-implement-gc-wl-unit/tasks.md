## 1. Rename And Rewire The GC/WL Controller

- [x] 1.1 Modify `flash_sim/FTL.py` to rename `GC_WL_Manager` to `GC_WL_Unit`, update `FTL.__init__`, `FTL.Validate_construction`, and all block-manager / AMU / TSU references so the new class remains the single owner of GC and wear-leveling decisions.
- [x] 1.2 Modify `flash_sim/FTL.py::GC_WL_Unit.Validate_construction` and any related helper state so the controller explicitly validates the dependencies it now needs for GC, dynamic WL, and static WL flows.

## 2. Implement Dynamic Wear-Leveling Allocation

- [x] 2.1 Modify `flash_sim/FTL.py::Block_Manager.get_write_frontier`, `flash_sim/FTL.py::Block_Manager.allocate_gc_write_page`, and any new helper functions to select the lowest-erase eligible free block instead of consuming free blocks by implicit block index order.
- [x] 2.2 Modify `flash_sim/FTL.py::PlaneBKE`, `flash_sim/FTL.py::Block_Manager.finalize_gc_erase`, and any new GC/WL helper state so erased blocks are returned to a WL-aware free pool with up-to-date `wl_level` metadata.

## 3. Implement Static Wear-Leveling And Safety Rules

- [x] 3.1 Modify `flash_sim/FTL.py::GC_WL_Unit.check_gc`, `_trigger_gc`, `_gc`, and any new helper functions to distinguish GC victims, static-WL sources, relocation destinations, and safe-block eligibility checks.
- [x] 3.2 Modify `flash_sim/FTL.py::GC_WL_Unit`, `flash_sim/FTL.py::Block_Manager`, and any AMU / TSU barrier helpers to reuse existing transaction-chain locking for static wear-leveling relocations and to block unsafe conflicting work until relocation completes.
- [x] 3.3 Modify `flash_sim/FTL.py::Block_Manager.finalize_gc_erase` and the GC/WL post-erase path so each completed erase can evaluate and, when needed, submit a follow-up static wear-leveling migration chain on the same plane.

## 4. Verification

- [x] 4.1 Add or update focused regression tests under `tests/` that cover the `GC_WL_Unit` rename, dynamic WL lowest-erase free-block selection, unsafe-block exclusion, and static WL trigger / barrier behavior.
- [x] 4.2 Run the relevant automated test commands for the touched GC/WL and FTL paths, and confirm the new wear-leveling behavior passes without regressing existing GC functionality.
