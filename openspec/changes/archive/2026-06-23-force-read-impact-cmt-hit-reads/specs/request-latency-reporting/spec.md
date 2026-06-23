## ADDED Requirements

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
