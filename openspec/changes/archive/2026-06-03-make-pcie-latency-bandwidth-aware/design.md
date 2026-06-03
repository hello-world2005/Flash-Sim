## Context

The current event-driven simulator models PCIe as two serialized message queues, one for Host to Device and one for Device to Host. That serialization behavior is useful and should remain, but `flash_sim/pcie_link.py::PCIe_link.estimate_latency(...)` currently returns a fixed `100` for every message regardless of whether the message is a tiny control request or a large data payload.

That simplification now distorts end-to-end timing in visible ways:

- `WRITE_REQ`, `READ_REQ`, and `REQ_COMP` cost the same as `WRITE_DATA`, `SEARCH_DATA`, or `COMPUTE_DATA`.
- Different request sizes produce identical PCIe transfer delay even when the host is logically moving different amounts of user data.
- Queue-drain timing in each direction depends only on enqueue order, not on message size, so large transfers cannot back-pressure later messages.

The current code already gives us enough information to improve this without redesigning the request flow:

- data-bearing PCIe messages carry `payload["data"]`
- the simulator already defines `SECTOR_SIZE_BYTES` in `flash_sim/common.py`
- simulation time is tracked as integer nanoseconds

## Goals / Non-Goals

**Goals:**

- Make PCIe transfer latency depend on the amount of user data carried by each message.
- Include a fixed per-message PCIe packaging overhead so control/completion messages still consume non-zero link time.
- Preserve the current per-direction serialization model and future-`DELIVER` event scheduling pattern.
- Keep the latency calculation deterministic and compatible with the simulator's integer timebase.
- Add tests that distinguish control messages from data messages and small payloads from large payloads.

**Non-Goals:**

- Modeling full PCIe protocol behavior such as TLP fragmentation, replay, ACK/NAK, credits, or lane-level flow control.
- Changing Host, HIL, FTL, or PHY semantics beyond when PCIe delivery events occur.
- Redesigning trace inputs, request schemas, or completion contracts.
- Making PCIe timing depend on request type beyond the size information already present in each message.

## Decisions

### Decision: Compute PCIe latency from transfer bytes rather than message type

`PCIe_link.estimate_latency(...)` will stop returning a fixed constant and instead compute:

`ceil((user_data_bytes + fixed_packet_overhead_bytes) / pcie_interface_bandwidth_bytes_per_ns)`

The estimator remains local to `PCIe_link`, and both `send(...)` and queue-drain rescheduling continue to call the same function so each queued message gets a latency based on its own size.

Alternatives considered:

- Keep a fixed latency table keyed by `MessageType`. Rejected because it still hides the difference between small and large payloads of the same message type.
- Model several PCIe phases separately (submit, DMA setup, payload, completion). Rejected because it adds architectural complexity that the user did not ask for.

### Decision: Represent PCIe bandwidth in byte-based simulator units

The new constants should be stored in simulator-wide configuration as byte-based values that map directly to the existing nanosecond timebase:

- `PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS`
- `PCIE_PACKET_OVERHEAD_BYTES`

Using bytes-per-nanosecond avoids repeated bit/second conversions inside the hot path and keeps the design formula identical to the requested one once units are normalized.

Alternatives considered:

- Store bandwidth in GB/s and convert on every estimate. Rejected because it adds unnecessary floating-point conversion noise to a simple simulator timing path.
- Hard-code the constants inside `pcie_link.py`. Rejected because the values should remain visible and reusable at the simulator configuration level.

### Decision: Derive user data bytes from payload content, not just from message type names

The estimator will treat a message as data-bearing when `payload["data"]` exists. For the current simulator model:

- list-backed payloads represent sector-granularity user data, so `user_data_bytes = len(data) * SECTOR_SIZE_BYTES`
- byte-like payloads, if introduced later, can use `len(data)` directly
- request, control, and completion messages without `payload["data"]` contribute `0` user-data bytes

This makes the latency logic future-proof for other PCIe messages that may later carry data without forcing another message-type allowlist.

Alternatives considered:

- Maintain a hard-coded list of data-bearing `MessageType`s. Rejected because it is brittle and easy to forget when new messages are added.
- Use `Request.size` for every message. Rejected because many messages carry no request at all, and a completion message should not inherit transfer size from the original request.

### Decision: Round latency upward to the simulator time granularity

Because event times are integer nanoseconds, the estimated PCIe latency will use upward rounding so that any positive transfer size produces at least one unit of simulated delay when bandwidth is high. The fixed packet overhead ensures even control messages have a positive transfer size.

Alternatives considered:

- Floor the result to an integer. Rejected because small transfers could become zero-latency.
- Keep floating-point event times. Rejected because the rest of the simulator consistently uses integer scheduling.

### Decision: Keep per-direction queue serialization unchanged

The change intentionally affects only transfer-time estimation, not queue structure. Host-to-Device and Device-to-Host messages remain independently serialized, and the next queued message in a direction starts only after the previous one in that direction is delivered.

Alternatives considered:

- Let multiple messages overlap on the same direction if their sizes are small. Rejected because it changes the simulator's PCIe concurrency model rather than just improving its delay estimate.

## Design Rationale

The design stays as close as possible to the current simulator architecture. `PCIe_link` already owns message queueing and future `DELIVER` scheduling, so size-aware latency belongs there rather than in `Host`, `HIL`, or `Engine`.

Choosing payload-derived transfer size also matches how the simulator already represents data movement. The host creates `WRITE_DATA`, `SEARCH_DATA`, `COMPUTE_DATA`, and `STATIC_WRITE_DATA` messages with explicit payload lists, so the latency model can become more realistic without introducing a second parallel accounting path.

Finally, byte-based constants plus upward rounding keep the model easy to reason about. The user wants a simple formula, and this design preserves that simplicity while still fitting the simulator's discrete-event time model.

## Risks / Trade-offs

- [PCIe constants chosen poorly could dominate or understate NAND timing] -> Mitigation: keep bandwidth and overhead as explicit shared constants so they can be calibrated without changing logic.
- [Payload-size inference could be inconsistent if future messages carry non-sector data formats] -> Mitigation: centralize message-size estimation in `PCIe_link` and document how list-backed versus byte-backed payloads are interpreted.
- [Changing message delay will shift existing end-to-end timestamps and test expectations] -> Mitigation: update or add tests to assert relative timing behavior instead of hard-coding the old fixed `100ns`.
- [Control messages may still look unrealistically cheap compared with real PCIe setup cost] -> Mitigation: always include `PCIE_PACKET_OVERHEAD_BYTES` even when user data is absent.

## Migration Plan

1. Add OpenSpec delta requirements for bandwidth-aware PCIe message timing.
2. Introduce shared PCIe timing constants for interface bandwidth and fixed packet overhead.
3. Refactor `flash_sim/pcie_link.py::PCIe_link.estimate_latency(...)` to compute payload-aware transfer bytes and rounded latency.
4. Verify that both initial `send(...)` scheduling and queue-drain rescheduling use the new estimator consistently.
5. Add regression tests comparing control messages versus data messages and small versus large payloads.

Rollback is straightforward: restore the old fixed-latency estimator and remove the new constants. No persisted state or external interface migration is required.

## Open Questions

- None for proposal scope. If implementation reveals a need to distinguish list elements that are not sector-sized user data, that refinement can be handled inside the estimator without changing the proposal contract.
