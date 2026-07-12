# Align read mapping, PCIe, and validation artifacts

The event-driven simulator and the maintained 64 B-sector MQSim-test fork had diverged in PCIe payload timing and finite-CMT mapping behavior. Intermediate validation results also mixed warmup writes with measured reads and did not identify one authoritative final result.

This change aligns PCIe TLP accounting, makes read payload return a real queued event, restricts GMT to dirty departing entries, coalesces in-flight MVPN reads, fixes MQSim mapping-write dispatch/dependencies, converts Exchange traces with 64 B sectors on both sides, and records the final read-aligned result and remaining write limitation.
