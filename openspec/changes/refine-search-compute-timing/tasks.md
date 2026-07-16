## 1. Configuration and request model

- [x] 1.1 Modify `flash_sim/config.py` geometry defaults, validation, event-runtime construction, `FlashConfig.from_dict()`, and `FlashConfig.to_dict()` to expose the six search/compute configuration fields.
- [x] 1.2 Modify `flash_sim/common.py` to export active geometry constants and add request-level `selected_wl` without adding duplicate transaction fields.
- [x] 1.3 Modify `flash_sim/parser.py`, `flash_sim/engine.py`, and repository COMPUTE engine traces to require and construct `selected_wl`.
- [x] 1.4 Modify `flash_sim/HIL.py` request validation to reject missing or out-of-range COMPUTE WL selections before data fetch while retaining SSL-granularity segmentation.

## 2. Wave scheduling

- [x] 2.1 Modify `flash_sim/FTL.py::issue_search_command` to select one source request per die wave and at most one transaction per plane.
- [x] 2.2 Modify `flash_sim/FTL.py::issue_compute_command` to enforce die-level source-request/WL compatibility, per-plane active-SL limits, and one SSL per `(block, sl)` while allowing different SLs in one block.

## 3. ONFI timing payloads

- [x] 3.1 Modify `flash_sim/PHY.py` transfer tasks and payload helpers to freeze and use operation-specific directional byte counts.
- [x] 3.2 Modify SEARCH/COMPUTE enqueue paths in `flash_sim/PHY.py` to use WL keyword input, active-SL compute input, concatenated match output, and per-BL ADC compute output formulas without changing fixed array delays or arbitration.

## 4. Specifications and documentation

- [x] 4.1 Add OpenSpec delta requirements for configuration, request validation, wave scheduling, and ONFI payload behavior under `openspec/changes/refine-search-compute-timing/specs/`.
- [x] 4.2 Modify `docs/cim-cam.md` and `SESSION_BACKGROUND.md` to document the final timing-only address, wave, and payload semantics; retain cross-request die parallelism as a TODO.

## 5. Verification

- [x] 5.1 Run OpenSpec validation plus Python compile/import checks for all modified modules; do not add or modify automated tests in this step.
- [x] 5.2 Review the diff for unintended READ/WRITE/GC behavior changes and confirm deferred automated-test targets remain documented.

## 6. Automated tests

- [x] 6.1 Modify existing parser, request-error, trace-generator, and contention-experiment tests/helpers to supply and validate COMPUTE `selected_wl`.
- [x] 6.2 Extend `test_script/test_config.py` with CIM defaults, round-trip, and non-positive-value validation cases.
- [x] 6.3 Add `test_script/test_cim_cam_scheduling.py` for SSL-granularity segmentation and SEARCH/COMPUTE die-wave scheduling rules.
- [x] 6.4 Extend `test_script/test_onfi_channel_transfer_model.py` with exact SEARCH/COMPUTE directional payload and frozen-task assertions.
- [x] 6.5 Add an event-engine integration test covering valid COMPUTE completion and same-SL multi-wave execution.

## 7. Regression verification

- [x] 7.1 Run the focused CIM/request/config/ONFI tests and fix failures within the approved change scope.
- [x] 7.2 Run the complete `pytest test_script -q` suite, OpenSpec validation, compile checks, and diff checks.

## 8. Trace-level parallelism verification

- [x] 8.1 Add simple engine traces for COMPUTE same-SL serialization, different-SL and cross-plane parallelism, and cross-die request independence.
- [x] 8.2 Add simple engine traces for SEARCH same-plane serialization, cross-plane parallelism, same-die request isolation, and cross-die request independence.
- [x] 8.3 Add an event-engine test that classifies the trace scenarios by overlap of raw `phy_array_exec` intervals and run focused plus regression verification.

## 9. Full-die parallelism verification

- [x] 9.1 Add full-die COMPUTE and SEARCH traces covering all 2,048 SSL operation addresses of one event-runtime die.
- [x] 9.2 Extend the trace test to group raw array intervals by wave and assert COMPUTE has 4 waves of 512 transactions while SEARCH has 512 waves of 4 transactions.
- [x] 9.3 Verify full-die ONFI phase counts and durations, then run focused and complete regression checks.

## 10. Configuration and high-concurrency hardening

- [x] 10.1 Propagate `FlashConfig.onfi` and CIM geometry fields through Engine/Device into HIL, TSU, and PHY while retaining compact event-runtime address geometry.
- [x] 10.2 Add end-to-end tests for configured WL validation, payload sizes, ONFI duration, and `compute_max_parallel_sl` wave limits.
- [x] 10.3 Add full-chip COMPUTE and SEARCH traces and assert four-die peak array concurrency.
- [x] 10.4 Add COMPUTE static-result data-out command-preemption and exact-duration resume coverage.
- [x] 10.5 Run focused and complete regression, OpenSpec validation, compile, and diff checks.

## 11. User guide synchronization

- [x] 11.1 Update `usage.md` with event-engine CIM/ONFI configuration propagation, COMPUTE `selected_wl`, SSL-granularity addressing, wave scheduling, directional payload formulas, and runnable examples.
