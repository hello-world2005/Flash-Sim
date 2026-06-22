## 1. ONFI Timing Helpers

- [x] 1.1 Add ONFI/NVDDR2 timing defaults and channel-width fields in `flash_sim/common.py` or `flash_sim/config.py`, preserving deterministic behavior for callers that do not override configuration.
- [x] 1.2 Add pure timing helper functions in `flash_sim/PHY.py` or a small local helper module for MQSim-style read command, program command, erase command, data-in, and data-out duration calculation.
- [x] 1.3 Replace direct uses of fixed `PHY_CMD_ADDR_TIME`, `PHY_DATA_IN_TIME`, and `PHY_DATA_OUT_TIME` in `flash_sim/PHY.py` transfer scheduling with the new timing helpers while preserving array-execution timing constants.

## 2. Channel Transfer Scheduler

- [x] 2.1 Add a PHY-owned per-channel transfer task representation in `flash_sim/PHY.py` with task kind, priority, channel id, chip id, die id, transactions, total duration, remaining duration, start time, finish time, and completion event reference.
- [x] 2.2 Add per-channel active-transfer and pending-transfer state in `flash_sim/PHY.py`, replacing ad hoc `_channel_busy` writes where transfers start and finish.
- [x] 2.3 Add transfer classification helpers in `flash_sim/PHY.py` that map transaction/source/op kind into the priority buckets: command, search/write/compute data-in, GC write data-in, mapping data-out, user data-out, search/compute data-out, and GC read data-out.
- [x] 2.4 Update `flash_sim/FTL.py::_on_channel_idle` and related TSU activation paths to ask PHY to schedule the next channel transfer instead of directly forcing waiting data-out ahead of all commands.

## 3. Data-Out Preemption and Resume

- [x] 3.1 Add command-submission handling in `flash_sim/PHY.py` that preempts an active data-out transfer on the same channel when a command transfer becomes ready.
- [x] 3.2 When preempting data-out in `flash_sim/PHY.py`, mark the previously registered data-out completion `SimEvent` as `ignored`, compute remaining duration from current simulation time, and requeue the interrupted transfer.
- [x] 3.3 Add resume handling in `flash_sim/PHY.py` that restarts interrupted data-out with its remaining duration and registers a fresh completion event.
- [x] 3.4 Ensure ignored stale data-out events in `flash_sim/PHY.py::execute` do not complete transactions, mutate chip/die state, broadcast callbacks, or emit duplicate request-latency intervals.

## 4. Request Latency Reporting Integration

- [x] 4.1 Update `flash_sim/PHY.py` calls into `RequestLatencyRecorder` so command, data-in, and data-out intervals are recorded for actual transfer segments after priority scheduling.
- [x] 4.2 Ensure preempted data-out appears in JSON reports as multiple `phy_data_out` intervals separated by the interrupting command interval, without counting the preemption gap.
- [x] 4.3 Audit `flash_sim/request_latency_report.py` only if needed to support split intervals; do not change request latency report field names.

## 5. Verification

- [x] 5.1 Add focused tests under `test_script/` for NVDDR2 timing helper calculations, including payload-size scaling, plane-count command scaling, and positive non-zero durations.
- [x] 5.2 Add PHY scheduler tests under `test_script/` for the exact requested transfer priority order.
- [x] 5.3 Add a data-out preemption test under `test_script/` that verifies a command interrupts active user data-out, the old completion event is ignored, and the resumed data-out completes after the remaining duration.
- [x] 5.4 Add a request latency report test under `test_script/` proving preempted data-out is reported as split non-overlapping `phy_data_out` intervals.
- [x] 5.5 Run focused PHY/TSU tests, request latency report tests, and the read-impact experiment or its regression test to observe the changed ONFI contention behavior.
