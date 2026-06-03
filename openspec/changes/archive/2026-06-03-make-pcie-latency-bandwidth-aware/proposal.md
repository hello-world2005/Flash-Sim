## Why

The simulator currently models every PCIe transfer with the same fixed latency, so a tiny control message and a large write payload arrive at the same simulated time cost. That makes request timing insensitive to transferred data volume and hides the difference between control-path traffic and bulk data movement.

## What Changes

- Replace the fixed PCIe message latency model with a bandwidth-aware transfer-time calculation based on message size.
- Define PCIe transfer size as `user data bytes + fixed PCIe packet overhead bytes`, so control-only messages still consume non-zero link time.
- Make data-bearing PCIe messages derive their user-data contribution from the payload they already carry, while request/completion messages use only the fixed overhead.
- Keep the existing per-direction serialization model and `DELIVER` event scheduling behavior, but make each scheduled delivery time depend on the specific message being transmitted.
- Add regression coverage showing that different payload sizes produce different PCIe delays and that control messages still incur the configured minimum transfer time.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `host-device-request-flow`: PCIe link delivery timing changes from a fixed constant to a transfer-size-based latency model using payload bytes, fixed packet overhead, and configured interface bandwidth.

## Non-goals

- Changing Host, HIL, FTL, or PHY request semantics beyond the timing of PCIe message delivery.
- Modeling PCIe protocol details such as TLP fragmentation, credits, retries, lane training, or separate command/data channels.
- Changing flash-media timing constants, TSU scheduling policy, or request completion criteria.
- Redesigning the trace format or adding per-request PCIe timing overrides.

## Impact

- In scope modules/functions: `flash_sim/pcie_link.py::PCIe_link.send`, `flash_sim/pcie_link.py::PCIe_link.estimate_latency`, `flash_sim/pcie_link.py::PCIe_link.execute`, and shared constants/config in `flash_sim/common.py` or another simulator-wide configuration module if needed for PCIe bandwidth and packet overhead.
- In scope tests: PCIe-link unit or integration tests that compare control versus data-bearing message delay, plus an end-to-end trace target that issues different-sized writes and confirms distinct delivery timestamps.
- Out of scope: `flash_sim/FTL.py`, `flash_sim/HIL.py`, NAND timing in `flash_sim/PHY.py`, and non-event-driven simulator paths unless they directly read the new PCIe constants.
