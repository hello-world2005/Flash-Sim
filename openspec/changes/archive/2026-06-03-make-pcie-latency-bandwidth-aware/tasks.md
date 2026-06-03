## 1. PCIe Timing Configuration

- [x] 1.1 Modify `flash_sim/common.py` to add shared PCIe timing constants for `PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS` and `PCIE_PACKET_OVERHEAD_BYTES` (or equivalent names), with units documented against the simulator's nanosecond timebase.
- [x] 1.2 Modify `flash_sim/pcie_link.py` imports and related constant usage so `PCIe_link` reads the new shared PCIe timing configuration together with existing payload-size helpers such as `SECTOR_SIZE_BYTES`.

## 2. Payload-Aware PCIe Latency

- [x] 2.1 Modify `flash_sim/pcie_link.py::PCIe_link.estimate_latency` to derive user-data bytes from `message.payload["data"]` when present, add the fixed PCIe packet overhead, divide by interface bandwidth, and round the result up to an integer simulation delay.
- [x] 2.2 Modify `flash_sim/pcie_link.py::PCIe_link.send` and `flash_sim/pcie_link.py::PCIe_link.execute` so both initial delivery scheduling and same-direction queue-drain rescheduling use the new per-message latency estimate without changing the existing Host-to-Device and Device-to-Host serialization behavior.

## 3. Verification

- [x] 3.1 Add or update tests under `tests/` (for example a dedicated `tests/test_pcie_link_latency.py`) to verify control/completion messages use overhead-only latency and data-bearing messages scale with payload size.
- [x] 3.2 Add or update an event-driven integration test or trace-driven regression that exercises different request sizes and verifies PCIe delivery timing differs accordingly while per-direction message ordering remains serialized.
