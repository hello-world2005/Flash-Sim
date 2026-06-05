## MODIFIED Requirements

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
