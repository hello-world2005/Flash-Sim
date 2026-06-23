## Why

The current PHY channel model treats ONFI command, data-in, and data-out transfers with coarse fixed delays and largely non-preemptive channel occupancy. This hides the contention pattern seen in read-impact experiments, where read completion depends on the relative priority of commands, data-in, and data-out work on the same channel.

## What Changes

- Add an ONFI channel transfer scheduler that chooses the next transfer by explicit channel-task priority:
  1. command
  2. search/write/compute data-in
  3. GC write data-in
  4. mapping data-out
  5. user data-out
  6. search/compute data-out
  7. GC read data-out
- Allow a newly available command transfer to preempt any active lower-priority ONFI transfer task on the same channel, including data-in and data-out tasks.
- Use the simulator event `ignored` mechanism to cancel the previously registered lower-priority transfer completion event when a task is preempted, record the remaining transfer duration, and re-register a completion event when that transfer task resumes.
- Replace fixed ONFI transfer constants with a MQSim-inspired NVDDR2 model based on command/address protocol timings, data transfer sizes, channel width, and ONFI timing parameters.
- Preserve request-latency reporting explainability by recording split transfer intervals and command/data-in/data-out timings after preemption.
- Add focused tests for priority ordering, command preemption/resume of lower-priority transfers, ignored stale completion events, and NVDDR2 delay calculations.
- No breaking changes to trace input format or request latency report schema are intended.

## Capabilities

### New Capabilities

- `onfi-channel-transfer-model`: Defines channel-level ONFI transfer arbitration, command preemption/resume behavior for lower-priority transfers, and MQSim-style NVDDR2 command/data transfer latency calculation.

### Modified Capabilities

- None.

## Non-goals

- This change does not reimplement MQSim wholesale or add every MQSim flash protocol feature.
- This change does not alter host PCIe timing, host SQ/CQ behavior, trace parsing, or request latency report field names.
- This change does not redesign AMU mapping policy, CMT/GMT behavior, GC victim selection, or static wear-leveling policy.
- This change does not add command preemption of array execution beyond existing write/erase suspend semantics; the new preemption is limited to ONFI channel transfer tasks that have lower priority than command.

## Impact

- In scope modules and functions:
  - `flash_sim/PHY.py`: channel transfer state, command/data-in/data-out event scheduling, lower-priority transfer preemption/resume, and latency recorder interval emission.
  - `flash_sim/FTL.py`: TSU channel-idle handling and channel transfer priority integration where PHY exposes pending transfer queues.
  - `flash_sim/common.py`: ONFI/NVDDR2 timing constants, transfer task types, and event metadata if needed.
  - `flash_sim/config.py`: optional configuration fields for channel width and ONFI timing parameters if not already present.
  - `test_script/`: regression tests for ONFI channel arbitration and transfer latency.
- Out of scope modules and functions:
  - `flash_sim/Host.py`, `flash_sim/PCIe_link.py`, standalone `flash_sim/simulator.py`, parser schemas, and experiment result consumers unless tests require small fixture updates.
- MQSim reference targets:
  - `../MQSim/src/ssd/ONFI_Channel_NVDDR2.h`
  - `../MQSim/src/ssd/ONFI_Channel_NVDDR2.cpp`
  - `../MQSim/src/ssd/NVM_PHY_ONFI_NVDDR2.cpp`
  - `../MQSim/src/ssd/TSU_Base.cpp`
  - `../MQSim/src/ssd/TSU_OutOfOrder.cpp`
- Test targets:
  - A channel with active user data-out or active data-in receives a new read/write/search/compute command and must split the interrupted transfer interval around the command transfer.
  - Pending mapping data-out must be chosen before user data-out, and user data-out before search/compute data-out and GC read data-out.
  - A stale completion event for a preempted lower-priority transfer marked `ignored` must not complete the request early.
  - NVDDR2 transfer times must be derived from payload size and timing parameters rather than fixed `PHY_DATA_IN_TIME` / `PHY_DATA_OUT_TIME` values.
