## Context

The current simulator models ONFI channel occupancy inside `flash_sim/PHY.py` using coarse `_channel_busy` flags and fixed transfer constants such as `PHY_DATA_IN_TIME` and `PHY_DATA_OUT_TIME`. `PHY.send_command_to_chip(...)` immediately registers command-transfer events, and `_transfer_data(...)` immediately registers data-out completion events. `FTL.TSU._on_channel_idle(...)` currently prefers waiting data-out before trying new command activation.

MQSim's NVDDR2 path separates channel status from chip execution state. In `../MQSim/src/ssd/ONFI_Channel_NVDDR2.h`, command and data transfer delays are derived from NVDDR2 timing parameters and channel width. In `NVM_PHY_ONFI_NVDDR2.cpp`, command/address transfer, command+data-in transfer, and read data-out are separate bus occupations, and completed read data-out queues are prioritized as mapping read, user read, and GC read when the channel becomes available.

This change brings that shape into Flash-Sim while also adding the user-requested rule that commands may preempt any active ONFI transfer task with lower priority than command.

## Goals / Non-Goals

**Goals:**

- Make channel transfers explicit objects with task kind, priority, channel, target chip/die, transaction batch, total duration, remaining duration, and associated completion event.
- Schedule pending channel transfers by the requested priority order:
  `command` > `search/write/compute_data_in` > `gc_write_data_in` > `mapping_data_out` > `user_data_out` > `search/compute_data_out` > `gc_read_data_out`.
- Allow command transfers to preempt any active lower-priority transfer task on the same channel.
- Use `SimEvent.ignored` to invalidate a superseded lower-priority transfer completion event when preempting.
- Resume interrupted transfer tasks by re-registering a completion event with the remaining transfer time.
- Replace fixed ONFI transfer durations with MQSim-inspired NVDDR2 formulas based on payload bytes, channel width, plane count, and ONFI timing parameters.
- Keep request latency reports explainable by recording command/data-in/data-out intervals for the actual transfer segments that occur.

**Non-Goals:**

- Do not port MQSim's full controller implementation or XML configuration stack.
- Do not add read-array or write-array preemption beyond existing write/erase suspension semantics.
- Do not change host PCIe behavior or trace schemas.
- Do not add visualizations or experiment tooling changes in this proposal.

## Decisions

1. Introduce a PHY-owned per-channel transfer scheduler.

   Each channel should keep:
   - `active_transfer`: the transfer currently occupying the ONFI channel.
   - `pending_transfers`: queues or a priority queue of waiting transfers.
   - enough metadata to resume an interrupted transfer and ignore its stale event.

   Rationale: command preemption requires knowing what lower-priority transfer is active, which event will complete it, how much time has already elapsed, and which work is waiting. Encoding that in scattered `_channel_busy` booleans would make correctness fragile.

   Alternative considered: keep `_channel_busy` and add special cases in `_on_channel_idle`. That would not reliably handle mid-transfer command arrival because the preemption point happens while the channel is busy.

2. Use transfer kinds instead of transaction types as the channel priority key.

   Proposed ordered kinds:
   - `COMMAND`
   - `USER_DATA_IN` for search/write/compute data-in
   - `GC_WRITE_DATA_IN`
   - `MAPPING_DATA_OUT`
   - `USER_DATA_OUT`
   - `STATIC_RESULT_DATA_OUT` for search/compute result return from chip to controller
   - `GC_READ_DATA_OUT`

   Rationale: ONFI channel contention is about bus phases, not only FTL transaction classes. A write command and write data-in may be scheduled as one combined transfer for a simple implementation, but they still need priority classification that matches the requested order.

   Alternative considered: reuse existing `TransactionType` ordering. It cannot distinguish command transfer from data-in/data-out transfer phases and would not express the requested priority list.

3. Preempt when a submitted command outranks the active transfer.

   When a command transfer is submitted while the channel is occupied by a lower-priority transfer task, the active task should store `remaining_time = old_finish_time - now`, mark the old completion event ignored, and become pending again. The command then runs to completion, after which normal priority selection can resume the interrupted task or choose another pending transfer according to priority. Commands do not preempt active commands because they have equal priority.

   Rationale: this directly implements the requested command-preemption rule while preserving command atomicity and keeping lower-priority tasks from preempting each other.

   Alternative considered: keep the earlier data-out-only preemption rule. That still lets read data return be interrupted, but it does not satisfy the requirement that commands can interrupt any lower-priority channel transfer.

4. Model NVDDR2 transfer time with local helper functions.

   The implementation should add helpers equivalent in spirit to MQSim:
   - data-in transfer time: `ceil(payload_bytes / channel_width_bytes / 2) * two_unit_data_in_time`
   - data-out transfer time: `ceil(payload_bytes / channel_width_bytes / 2) * two_unit_data_out_time`
   - read command time by plane count, based on `t_CS`, `t_WC`, `t_WB`, `t_RR`, and `t_DBSY`
   - program command time by plane count, based on `t_CS`, `t_WC`, `t_ADL`, `t_WPST`, `t_WPSTH`, `t_CALS`, `t_WB`, and `t_DBSY`
   - erase command time by plane count, based on `t_CS`, `t_WC`, `t_WB`, and `t_DBSY`

   Rationale: MQSim already provides a compact NVDDR2 timing model in `ONFI_Channel_NVDDR2`. Flash-Sim should use the same parameter family while preserving Python-side validation and positive non-zero durations for non-zero payloads.

   Alternative considered: keep fixed constants and only add preemption. That would fix ordering but keep the ONFI model too coarse for size-sensitive experiments.

5. Keep timing configuration centralized.

   Default timing constants can live in `flash_sim/common.py` initially, but if `config.py` already exposes geometry/timing knobs for similar values, the implementation should add optional `FlashConfig`/`TimingConfig` fields and derive common defaults from them.

   Rationale: tests need deterministic defaults, while experiments may need to vary channel width and ONFI timings.

   Alternative considered: hard-code MQSim defaults inside `PHY.py`. That would make tests pass but make the model difficult to tune.

6. Preserve request latency intervals as actual segments.

   When a lower-priority transfer is preempted, the report should contain separate intervals for the completed segments of that phase, not one interval spanning the interruption. Preempted data-in should split `phy_data_in`; preempted data-out should split `phy_data_out`; command phases should be recorded only for the time the channel actually transfers them.

   Rationale: existing reports are used to explain contention. A single continuous transfer interval over a preemption would hide exactly the behavior this change adds.

## Design Rationale

This design moves ONFI transfer arbitration down into PHY because PHY owns channel busy state, event registration, chip/die active command state, and request-latency interval emission. TSU should continue to decide which transactions are eligible to issue, but PHY should decide when channel bus phases can actually run and whether an active lower-priority transfer phase must be paused.

MQSim is used as a reference for timing formulas and state separation, not as a line-by-line port. The user-requested command-over-lower-priority-transfer preemption is stricter than MQSim's default channel-busy guard, so the Flash-Sim model needs an explicit preemption path rather than relying only on MQSim's idle callback order.

## Risks / Trade-offs

- [Risk] Preempting lower-priority transfers can create stale completion events that complete a request twice or apply data-in/data-out side effects early. -> Mitigation: store the completion `SimEvent` object on the active transfer and mark it `ignored` before requeueing the remaining transfer.
- [Risk] Latency reporting may overcount interrupted transfer phases if intervals are not split. -> Mitigation: record PHY data-in/data-out intervals per actual transfer segment and add tests that assert no interval spans a command preemption.
- [Risk] New priority ordering can change existing e2e latency baselines. -> Mitigation: add focused tests for the new ordering and update only expectations that depend on ONFI channel arbitration.
- [Risk] MQSim timing formulas may produce zero duration for tiny payloads if translated with integer truncation. -> Mitigation: use a ceil-based two-unit count and validate that non-zero payloads produce positive transfer time.
- [Risk] Combining command and data-in as a single event can blur the requested priority distinction. -> Mitigation: either split command and data-in into separate transfer tasks or store sub-phase boundaries so command remains the highest-priority preemptor and data-in uses the correct class.
- [Risk] Adding configurable timing fields can drift from existing constants. -> Mitigation: preserve existing defaults where possible, document MQSim-derived defaults, and keep all ONFI timing helper tests pinned to those defaults.

## Migration Plan

1. Add ONFI transfer timing constants/helpers and tests for MQSim-style calculations.
2. Add PHY channel transfer state while keeping the old code path covered by tests during the transition.
3. Route command/data-in/data-out scheduling through the new transfer scheduler.
4. Add lower-priority transfer preemption/resume and ignored-event tests.
5. Run focused PHY/TSU tests plus request latency e2e tests affected by ONFI timing.
6. No data migration is required; generated reports may contain different timing values because the simulation model becomes more detailed.

## Open Questions

- Should user write data-in and search/compute data-in share exactly one priority bucket, as requested, even though search/compute are static-chip operations?
- Should mapping write data-in be classified with ordinary write data-in or GC write data-in? The proposed implementation should treat non-GC mapping writes as the ordinary data-in bucket unless a future requirement says otherwise.
- Should read data-out setup time (`ReadDataOutSetupTime` and multiplane variants in MQSim) be included in the data-out transfer duration immediately, or tracked as a separate setup sub-phase? The first implementation can fold it into data-out if tests document that choice.
