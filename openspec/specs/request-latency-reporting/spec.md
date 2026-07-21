# Request Latency Reporting Specification

## Purpose

Define the request-level latency reports produced by the event-driven simulator, including the structured JSON export, the spreadsheet-friendly CSV export, and the timing derivation rules for host, PCIe, mapping, scheduling, cache, and media-execution stages.

## Requirements

### Requirement: Event-driven simulation exports one latency report entry per input request

After an event-driven simulation completes, the system SHALL write request-level latency reports under `report/`. The simulator MUST keep the JSON report as the detailed source of truth and MUST also emit a companion CSV table for the same trace. Both outputs MUST contain one record per input trace request in trace order. The JSON report meta object MUST also expose maintenance-level GC/WL/write-backpressure statistics collected during the run.

#### Scenario: Successful simulation writes per-request JSON and CSV reports

- **WHEN** `Engine.Start_simulation(trace_path)` completes for a trace containing one or more requests
- **THEN** the system MUST write a `*_request_latency.json` report and a `*_request_latency.csv` report under `report/`, and each output MUST contain one entry for every input trace request

### Requirement: JSON request reports include request size

Each JSON request latency record SHALL include a top-level `size` field whose value matches the original input `Request.size` for that request.

#### Scenario: Generated JSON preserves input request sizes

- **WHEN** an event-driven simulation completes for a trace containing requests with `size` values
- **THEN** each entry in the generated JSON report's `requests` array MUST include `size` equal to the corresponding input trace request size

#### Scenario: JSON report includes maintenance summary

- **WHEN** an event-driven simulation completes
- **THEN** the JSON report `meta.maintenance` object MUST include GC count, static-WL count, relocated page count, erased block count, host write pages, physical user write pages, physical GC write pages, write amplification, minimum observed free pool, maximum observed wear skew, waiting-write counts, and backpressure wait time

### Requirement: JSON request reports include host, PCIe, AMU, TSU, and PHY stage breakdowns

Each JSON request record MUST include a stage breakdown with at least `host_sq_wait`, `host_dispatch`, `pcie_host_to_device`, `pcie_device_to_host`, `pcie_host_to_device_queue_wait`, `pcie_device_to_host_queue_wait`, `pcie_host_to_device_wire`, `pcie_device_to_host_wire`, `amu_mapping_wait`, `tsu_queue_wait`, `phy_channel_wait`, `phy_cmd_addr`, `phy_data_in`, `phy_array_exec`, and `phy_data_out`. The directional PCIe queue stages MUST cover enqueue-to-service-start, while the directional wire stages MUST cover service-start-to-delivery. These detail stages MUST NOT be counted again when reconciling their parent enqueue-to-delivery PCIe intervals. `phy_channel_wait` MUST contain time for which an ONFI command, data-in, or data-out task is pending but not active on its channel. If a request does not pass through a stage, that stage value MUST remain `0` instead of being omitted.

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

### Requirement: CSV latency table flattens each request into a raw host-visible completion row

The CSV report SHALL flatten each request into one row with a fixed, subsystem-grouped column order. Identity columns MUST come first. Host/controller columns MUST be followed by all PCIe columns, then NAND/ONFI columns, energy columns, and finally status/maintenance columns. The exact order MUST be: `Issue Time`, `REQ Type`, `Finish Time`, `Time in SQ`, `Cache Hit`, `Mapping`, `Time in TSU`, `Backpressure Wait Time`, `PCIe Xfer`, `PCIe Queue (Host)`, `PCIe Queue (Device)`, `PCIe Wire`, `PCIe Xfer (Data)`, `PCIe Xfer (CQ)`, `ONFI Xfer`, `ONFI Service`, `Array Exec`, `Energy for req (μJ)`, `Energy for persistant storage (μJ)`, `Status`, `GC Count`, `GC Relocated Pages`, `GC Erased Blocks`, and `Write Amplification`. `ONFI Service` MUST contain only active command/data transfer intervals. The two PCIe queue columns MUST report raw waiting intervals in the H2D host-side FIFO and D2H device-side FIFO respectively. `PCIe Wire` MUST report the merged union of active H2D and D2H transfer intervals.

`completion_time` MUST be the absolute timestamp when the Host receives `REQ_COMP` for the request, not a derived latency value. The latency fields MUST be derived from recorded raw intervals rather than from an end-to-end residual. For a serial request path with no overlapping stages, the sum of `sq_wait_time`, `pcie_request_send_time`, `mapping_time`, `tsu_wait_time`, `phy_transfer_time`, `phy_array_time`, and `pcie_status_return_time` MUST equal `completion_time - issue_time`. Requests whose transactions execute in parallel MAY have overlapping CSV stage values; the JSON `overlap_latency` and `untracked_latency` fields remain the source of truth for reconciliation. `pcie_data_return_time` MUST remain a separate payload-return column and MUST NOT be added to the host-visible completion path.

For `SEARCH` and `COMPUTE`, `cache_hit` MUST be `/`.

The CSV report MAY append maintenance and status columns after the host-visible latency and energy columns. These appended columns MUST NOT participate in the additive host-visible latency equality.

#### Scenario: Serial completed read row is additive before payload return

- **WHEN** a completed `READ` request with a serial, non-overlapping execution path is exported to CSV
- **THEN** the sum of every latency column except `pcie_data_return_time` MUST equal `completion_time - issue_time`

#### Scenario: Host-visible completion time is a timestamp

- **WHEN** a completed request is exported to CSV
- **THEN** `completion_time` MUST equal the timestamp when the Host receives `REQ_COMP`

#### Scenario: CSV appends machine-readable maintenance columns

- **WHEN** a completed simulation is exported to CSV
- **THEN** each CSV row MUST include request status and the run-level GC count, relocated page count, erased block count, write amplification, and backpressure wait time in appended columns

### Requirement: Maintenance reporting tracks GC, WL, write amplification, and backpressure

The reporting subsystem SHALL track maintenance events independently from host-visible request latency. `GC_WL_Unit` and `Block_Manager` MUST report successful GC/static-WL relocation starts, GC erase completions, physical user writes, physical GC writes, plane free-pool snapshots, wear-skew snapshots, and write-backpressure enqueue/retry events. Write amplification MUST be calculated as `(physical_user_write_pages + physical_gc_write_pages) / host_write_pages`, with `0.0` when no host write pages exist.

#### Scenario: Aborted relocation setup is not reported as a GC start

- **WHEN** GC or static-WL relocation setup fails before the complete transaction chain is submitted, including failure to resolve a source page LPA
- **THEN** the reporting subsystem MUST NOT increment the GC/static-WL start count or relocated-page count for that aborted setup

#### Scenario: Backpressure waiting time is accumulated on retry

- **WHEN** a write transaction enters a per-plane waiting queue and later successfully retries into `TSU`
- **THEN** the report MUST add the elapsed time between enqueue and successful retry to `backpressure_wait_time`

#### Scenario: Physical writes contribute to write amplification

- **WHEN** user writes and GC relocation writes complete at the PHY/block-manager path
- **THEN** the report MUST count user writes and GC writes separately and use both counts to derive write amplification

#### Scenario: Failed relocation setup is not counted as a started GC

- **WHEN** GC or static-WL candidate selection finds a victim but cannot reserve a valid relocation destination
- **THEN** the report MUST NOT increment GC/static-WL start counters for that aborted relocation setup

#### Scenario: Completed static WL conserves maintenance events

- **WHEN** one static-WL relocation chain completes through `PHY`
- **THEN** `static_wl_count` MUST increase once, `gc_relocated_pages` MUST equal the completed physical GC writes for that chain, and `gc_erased_blocks` MUST include its completed source erase

### Requirement: Mapping time absorbs the full mapping dependency path

For requests that rely on `MAPPING_READ`, `mapping_time` MUST absorb the full mapping-resolution phase from the end of request-side PCIe ingress through the end of the mapping dependency. This includes any explicit request-level mapping wait, any `MAPPING_READ` TSU queueing, and any `MAPPING_READ` PHY command, transfer, array, and data-out time.

Any time counted in `mapping_time` MUST NOT be counted again in `tsu_wait_time`, `phy_transfer_time`, or `phy_array_time`.

#### Scenario: Mapping-dependent read reports mapping work even without explicit AMU wait

- **WHEN** a `READ` request has related `MAPPING_READ` TSU/PHY activity but no explicit `amu_mapping_wait` interval
- **THEN** the CSV row MUST still report `mapping_time > 0`

#### Scenario: Controller-cache read keeps mapping and PHY stages at zero

- **WHEN** a `READ` request is fully satisfied by controller-side cache without backend execution
- **THEN** the CSV row MUST report `cache_hit` as a hit, `mapping_time=0`, `phy_transfer_time=0`, and `phy_array_time=0`

### Requirement: TSU wait reports only recorded USER transaction queueing

`tsu_wait_time` MUST be the merged duration of raw host-visible USER transaction intervals from TSU enqueue until TSU dispatch. It MUST NOT extend to the later PHY command-transfer start and therefore MUST NOT absorb PHY channel queueing.

#### Scenario: PHY command queueing does not leak into TSU wait

- **WHEN** a USER transaction is dispatched by TSU but its ONFI command task waits for the channel
- **THEN** that post-dispatch wait MUST be reported through `phy_channel_wait` and `ONFI Xfer`, not through `tsu_wait_time`

#### Scenario: Mapping dependency wait does not leak into USER TSU wait

- **WHEN** a mapping-miss `READ` spends time in `MAPPING_READ` queueing before its `USER_READ` can start
- **THEN** that dependency time MUST be reflected in `mapping_time` rather than `tsu_wait_time`

### Requirement: ONFI Xfer includes channel queueing and active service

The CSV `ONFI Xfer` value MUST be the merged duration of host-visible USER command, data-in, and data-out intervals from each PHY channel-task enqueue through that task's active transfer completion. It MUST include channel queueing. It MUST NOT include NAND array execution between command completion and data-out enqueue. `ONFI Service` MUST include only the active `phy_cmd_addr`, `phy_data_in`, and `phy_data_out` intervals and MUST exclude `phy_channel_wait`.

#### Scenario: Read data-out waits for a busy channel

- **WHEN** a read array operation finishes and its data-out task waits before becoming active on the ONFI channel
- **THEN** `ONFI Xfer` MUST include that wait
- **AND** `ONFI Service` MUST exclude it
- **AND** `phy_channel_wait` MUST record the raw wait interval in JSON

### Requirement: CSV return reporting separates status latency from payload latency

The CSV MUST split the final PCIe return reporting into `pcie_status_return_time` and `pcie_data_return_time`. `pcie_status_return_time` MUST represent the `REQ_COMP`/CQ path that determines host-visible completion. `pcie_data_return_time` MUST represent the additional response payload transfer for read-like requests and MUST be `0` for writes that do not return payload data.

#### Scenario: Read row separates completion status from returned data

- **WHEN** a `READ` request is exported to CSV
- **THEN** the CSV row MUST report the `REQ_COMP` path in `pcie_status_return_time` and MUST report the returned data transfer in `pcie_data_return_time`

#### Scenario: Write row has status return but no response payload return

- **WHEN** a `WRITE` request is exported to CSV
- **THEN** the CSV row MUST report host-visible completion in `pcie_status_return_time` and MUST set `pcie_data_return_time=0`

### Requirement: JSON request reports expose mapping-resolution counts

Each JSON request latency record SHALL include a top-level `mapping_resolution_counts` object with integer counts for `cmt_hit`, `gmt_hit`, `mapping_read`, and `uncached_write`. These counts MUST reflect the mapping-resolution events attributed to that request during simulation and MUST be present even when every count is zero.

#### Scenario: CMT-hit read reports CMT count

- **WHEN** a `READ` request resolves all required user mappings from CMT
- **THEN** the JSON request record MUST include `mapping_resolution_counts.cmt_hit` greater than `0`
- **AND** `mapping_resolution_counts.mapping_read` MUST equal `0`

#### Scenario: Mapping-read path reports mapping-read count

- **WHEN** a `READ` request requires one or more `MAPPING_READ` transactions before its user read can execute
- **THEN** the JSON request record MUST include `mapping_resolution_counts.mapping_read` greater than `0`

#### Scenario: Non-mapping request keeps zero counts

- **WHEN** a `COMPUTE` or `SEARCH` request is exported to the JSON latency report
- **THEN** the JSON request record MUST include `mapping_resolution_counts` with `cmt_hit`, `gmt_hit`, `mapping_read`, and `uncached_write` all equal to `0`
