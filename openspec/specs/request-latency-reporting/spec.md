# Request Latency Reporting Specification

## Purpose

Define the request-level latency reports produced by the event-driven simulator, including the structured JSON export, the spreadsheet-friendly CSV export, and the timing derivation rules for host, PCIe, mapping, scheduling, cache, and media-execution stages.

## Requirements

### Requirement: Event-driven simulation exports one latency report entry per input request

After an event-driven simulation completes, the system SHALL write request-level latency reports under `report/`. The simulator MUST keep the JSON report as the detailed source of truth and MUST also emit a companion CSV table for the same trace. Both outputs MUST contain one record per input trace request in trace order.

#### Scenario: Successful simulation writes per-request JSON and CSV reports

- **WHEN** `Engine.Start_simulation(trace_path)` completes for a trace containing one or more requests
- **THEN** the system MUST write a `*_request_latency.json` report and a `*_request_latency.csv` report under `report/`, and each output MUST contain one entry for every input trace request

### Requirement: JSON request reports include request size

Each JSON request latency record SHALL include a top-level `size` field whose value matches the original input `Request.size` for that request.

#### Scenario: Generated JSON preserves input request sizes

- **WHEN** an event-driven simulation completes for a trace containing requests with `size` values
- **THEN** each entry in the generated JSON report's `requests` array MUST include `size` equal to the corresponding input trace request size

### Requirement: JSON request reports include host, PCIe, AMU, TSU, and PHY stage breakdowns

Each JSON request record MUST include a stage breakdown with at least `host_sq_wait`, `host_dispatch`, `pcie_host_to_device`, `pcie_device_to_host`, `amu_mapping_wait`, `tsu_queue_wait`, `phy_cmd_addr`, `phy_data_in`, `phy_array_exec`, and `phy_data_out`. If a request does not pass through a stage, that stage value MUST remain `0` instead of being omitted.

#### Scenario: Read request with mapping miss reports AMU and PHY stages

- **WHEN** a `READ` request triggers a `MAPPING_READ` before its user read can execute
- **THEN** the JSON report for that request MUST contain non-zero `amu_mapping_wait`, `tsu_queue_wait`, `phy_cmd_addr`, `phy_array_exec`, and `phy_data_out` values

#### Scenario: Request bypasses a stage

- **WHEN** a request never enters one of the tracked stages
- **THEN** the JSON report MUST still include that stage in the breakdown and MUST set its value to `0`

### Requirement: Breakdown totals remain explainable under parallel execution

The reporting subsystem MUST summarize stage durations in a way that remains explainable when the same request overlaps across multiple chips, dies, or internal phases. The report MUST expose `overlap_latency` and `untracked_latency` so that the request total latency can be reconciled with the tracked stage intervals.

#### Scenario: Parallel transactions create overlap

- **WHEN** a single request has stage intervals that overlap in time because different transactions run in parallel
- **THEN** the report MUST use `overlap_latency` and `untracked_latency` to explain the difference between the summed stage durations and the request total latency

### Requirement: Buffered writes distinguish host-visible completion from media persistence

For `WRITE` and `STATIC_WRITE`, the report MUST distinguish host-visible completion from later backend persistence. The JSON record MUST preserve the host-visible completion time and host-facing breakdown, and it MUST also expose `persistence_status`, `persistence_total_latency`, and persistence-side TSU and PHY timings when media persistence occurs later.

#### Scenario: Cached write later reaches NAND

- **WHEN** a `WRITE` request completes at the host and the cached data is later flushed to flash media
- **THEN** the JSON report MUST include the host-visible completion timing and MUST also include `persistence_status="persisted"` together with non-zero persistence-side TSU and PHY timings

#### Scenario: Cached write is superseded before flush

- **WHEN** a cached write is overwritten before it receives an independent persistence path
- **THEN** the JSON report MUST keep the host-visible completion timing and MUST mark the request with `persistence_status="superseded_in_cache"`

### Requirement: CSV latency table flattens each request into an additive host-visible completion row

The CSV report SHALL flatten each request into one row with a fixed column order. The CSV MUST contain, from left to right: `issue_time`, `req_type`, `completion_time`, `sq_wait_time`, `pcie_request_send_time`, `cache_hit`, `mapping_time`, `tsu_wait_time`, `phy_transfer_time`, `phy_array_time`, `pcie_status_return_time`, and `pcie_data_return_time`.

`completion_time` MUST be the absolute timestamp when the Host receives `REQ_COMP` for the request, not a derived latency value. For any row with both `issue_time` and `completion_time`, the sum of `sq_wait_time`, `pcie_request_send_time`, `mapping_time`, `tsu_wait_time`, `phy_transfer_time`, `phy_array_time`, and `pcie_status_return_time` MUST equal `completion_time - issue_time`. `pcie_data_return_time` MUST remain a separate trailing payload-return column and MUST NOT be included in that equality.

For `SEARCH` and `COMPUTE`, `cache_hit` MUST be `/`.

#### Scenario: Completed read row is additive before payload return

- **WHEN** a completed `READ` request is exported to CSV
- **THEN** the sum of every latency column except `pcie_data_return_time` MUST equal `completion_time - issue_time`

#### Scenario: Host-visible completion time is a timestamp

- **WHEN** a completed request is exported to CSV
- **THEN** `completion_time` MUST equal the timestamp when the Host receives `REQ_COMP`

### Requirement: Mapping time absorbs the full mapping dependency path

For requests that rely on `MAPPING_READ`, `mapping_time` MUST absorb the full mapping-resolution phase from the end of request-side PCIe ingress through the end of the mapping dependency. This includes any explicit request-level mapping wait, any `MAPPING_READ` TSU queueing, and any `MAPPING_READ` PHY command, transfer, array, and data-out time.

Any time counted in `mapping_time` MUST NOT be counted again in `tsu_wait_time`, `phy_transfer_time`, or `phy_array_time`.

#### Scenario: Mapping-dependent read reports mapping work even without explicit AMU wait

- **WHEN** a `READ` request has related `MAPPING_READ` TSU/PHY activity but no explicit `amu_mapping_wait` interval
- **THEN** the CSV row MUST still report `mapping_time > 0`

#### Scenario: Controller-cache read keeps mapping and PHY stages at zero

- **WHEN** a `READ` request is fully satisfied by controller-side cache without backend execution
- **THEN** the CSV row MUST report `cache_hit` as a hit, `mapping_time=0`, `phy_transfer_time=0`, and `phy_array_time=0`

### Requirement: TSU wait reports only effective USER transaction queueing

`tsu_wait_time` MUST only measure the effective queueing of the host-visible USER transaction. It MUST start no earlier than the latest of:

- the first USER transaction submission to TSU
- the end of request-side PCIe ingress
- the end of the mapping phase

It MUST end when PHY begins issuing the first command for the corresponding USER transaction.

#### Scenario: Mapping dependency wait does not leak into USER TSU wait

- **WHEN** a mapping-miss `READ` spends time in `MAPPING_READ` queueing before its `USER_READ` can start
- **THEN** that dependency time MUST be reflected in `mapping_time` rather than `tsu_wait_time`

### Requirement: CSV return reporting separates status latency from payload latency

The CSV MUST split the final PCIe return reporting into `pcie_status_return_time` and `pcie_data_return_time`. `pcie_status_return_time` MUST represent the `REQ_COMP`/CQ path that determines host-visible completion. `pcie_data_return_time` MUST represent the additional response payload transfer for read-like requests and MUST be `0` for writes that do not return payload data.

#### Scenario: Read row separates completion status from returned data

- **WHEN** a `READ` request is exported to CSV
- **THEN** the CSV row MUST report the `REQ_COMP` path in `pcie_status_return_time` and MUST report the returned data transfer in `pcie_data_return_time`

#### Scenario: Write row has status return but no response payload return

- **WHEN** a `WRITE` request is exported to CSV
- **THEN** the CSV row MUST report host-visible completion in `pcie_status_return_time` and MUST set `pcie_data_return_time=0`
