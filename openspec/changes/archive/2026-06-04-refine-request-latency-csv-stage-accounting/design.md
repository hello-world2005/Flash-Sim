## Context

`flash_sim/request_latency_report.py` derives CSV rows from the recorder's request-level stage intervals. The raw recorder state mixes several kinds of timing that are useful for debugging but are not directly suitable as flat CSV columns:

- `amu_mapping_wait` only captures explicit request-level waiting for a mapping dependency.
- `tsu_queue_wait` records queueing for every transaction attributed to the request, including `MAPPING_READ`.
- PHY intervals contain both mapping-side transactions and host-visible user transactions.
- Host-visible completion is driven by `REQ_COMP`, while returned payload data can extend beyond that point.

The user now wants the CSV to separate these concepts more carefully without changing simulator scheduling.

## Goals / Non-Goals

**Goals:**

- Make completed CSV rows additive when summing every column except the final response-payload column.
- Fold the entire mapping-resolution dependency path into `mapping_time`.
- Restrict `tsu_wait_time` to the effective wait of the host-visible user transaction only.
- Keep `completion_time` host-visible and keep status-return and payload-return PCIe timing split.

**Non-Goals:**

- Changing AMU, TSU, or PHY execution order.
- Replacing the recorder model or removing the JSON overlap diagnostics.
- Reworking unrelated latency columns or non-reporting simulator behavior.

## Decisions

1. Define `mapping_time` as the full mapping-resolution phase.
   `mapping_time` is measured from the end of request-side PCIe ingress to the end of the request's mapping phase. The mapping phase includes:

- explicit request-level `amu_mapping_wait`
- `MAPPING_READ` TSU queueing
- `MAPPING_READ` PHY command/data-transfer/array/data-out work

   This means a request can have `mapping_time > 0` even when `amu_mapping_wait == 0`, as long as it still carries a related `MAPPING_READ` dependency path.

2. Define `tsu_wait_time` as USER transaction queueing only.
   `tsu_wait_time` starts at the latest of:

- the first USER transaction submission to TSU
- the end of request-side PCIe ingress
- the end of the mapping phase

   It ends when the first USER transaction begins PHY command issuance. This removes mapping-side queueing and any pre-ingress overlap from the USER TSU column.

3. Keep `pcie_request_send_time` as the residual host-visible front-end bucket.
   Some controller-side latency is not otherwise represented as a dedicated CSV stage, such as cache-side waiting before host-visible completion for certain reads. To keep completed rows additive, `pcie_request_send_time` remains the residual bucket after subtracting the other non-payload stages from `completion_time - issue_time`.

4. Keep `completion_time` and PCIe return semantics host-visible.
   `completion_time` remains the absolute timestamp when the Host receives `REQ_COMP`. `pcie_status_return_time` covers that completion path. `pcie_data_return_time` remains a separate trailing column for returned payload data and is intentionally excluded from the additive completion-path equality.

## Design Rationale

This approach matches the user's desired interpretation of the CSV while staying faithful to the simulator's existing behavior. The recorder already captures enough information to identify:

- where request ingress finishes
- where mapping resolution finishes
- where the first host-visible USER command begins
- when host-visible completion occurs

By deriving flattened CSV stages from those boundaries, we avoid double counting and keep the JSON report as the detailed source of overlapping raw intervals.

## Risks / Trade-offs

- [Risk] `pcie_request_send_time` is partly a residual bucket, so it can include controller-side latency beyond literal PCIe wire transfer. -> Mitigation: document the host-visible additive contract explicitly in the spec.
- [Risk] Users may compare raw JSON `tsu_queue_wait` to CSV `TSU中等待时间` and expect them to match. -> Mitigation: document that CSV TSU wait is USER-only, while raw JSON still contains all request-attributed TSU queueing.
- [Risk] Response-payload timing may still be estimated for some workloads. -> Mitigation: keep the existing explicit-message-first, estimate-fallback logic isolated in the report code.

## Migration Plan

1. Update the change spec to define additive host-visible completion rows, USER-only TSU wait, and mapping-phase absorption.
2. Derive CSV mapping, TSU, and PCIe columns from phase boundaries in `RequestLatencyRecorder`.
3. Refresh regression tests to cover mapping-dependent reads, cache-hit reads, returned-data requests, and additive real-trace rows.
4. Verify against focused fixtures and `test_case/test_trace.json`.

## Open Questions

- None.
