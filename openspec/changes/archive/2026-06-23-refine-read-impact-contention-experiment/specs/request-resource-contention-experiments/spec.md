## MODIFIED Requirements

### Requirement: Experiment tooling generates paired read-impact traces

The repository SHALL provide tooling to generate one baseline read-impact trace and one compute-contention trace for every configured read-impact scan condition. The read portions of the baseline trace and every compute-contention trace MUST be identical, including request count, order, `type`, `time`, `start_lha`, and `size`. Every measured `read` request MUST target exactly one page by using a page-aligned `start_lha` and a `size` equal to `SECTOR_PER_PAGE`. Each compute-contention trace MUST insert deterministic `compute` requests without changing any read command. Ratio-scan traces MUST use configured ratios `[0.1, 0.2, 0.4, 0.8]` with compute request size `128`. Request-size-scan traces MUST use configured compute request sizes `[8, 32, 128, 512]` with ratio `0.2`.

#### Scenario: Read portions are identical across all scan traces

- **WHEN** the read-impact trace generator creates the baseline trace, ratio-scan traces, and request-size-scan traces
- **THEN** filtering each trace to `read` requests MUST produce byte-for-byte identical read command arrays
- **AND** every filtered `read` command MUST have a page-aligned `start_lha`
- **AND** every filtered `read` command MUST have `size` equal to `SECTOR_PER_PAGE`

#### Scenario: Ratio scan inserts the requested compute ratios

- **WHEN** the read-impact trace generator creates ratio-scan compute-contention traces for `num_read_req` reads
- **THEN** it MUST create one trace for each ratio in `[0.1, 0.2, 0.4, 0.8]`
- **AND** each inserted `compute` request in those traces MUST have `size` equal to `128`
- **AND** each trace MUST include `max(1, round(num_read_req * ratio))` inserted `compute` requests for its configured ratio

#### Scenario: Request-size scan inserts the requested compute sizes

- **WHEN** the read-impact trace generator creates request-size-scan compute-contention traces for `num_read_req` reads
- **THEN** it MUST create one trace for each compute request size in `[8, 32, 128, 512]`
- **AND** each trace MUST use fixed ratio `0.2`
- **AND** each trace MUST include `max(1, round(num_read_req * 0.2))` inserted `compute` requests

### Requirement: Read-impact experiment compares read completion times

The read-impact experiment SHALL simulate the baseline trace and every compute-contention scan trace, then aggregate only `READ` request latency from the generated request latency JSON reports. For each condition, the result MUST include raw `average_read_latency`, `normalized_latency`, `num_read_req`, `num_compute_req`, the scan group, and the parameter value used for the condition. The baseline condition MUST have `num_compute_req` equal to `0` and `normalized_latency` equal to `1.0`. Every non-baseline condition MUST normalize its average read latency by dividing it by the baseline average read latency. Non-read inserted requests MUST NOT be included in average read latency.

#### Scenario: Baseline average read latency is the normalization base

- **WHEN** the baseline read-impact trace completes simulation successfully
- **THEN** the read-impact result MUST include a baseline condition row
- **AND** the baseline condition row MUST include the average latency of baseline `READ` entries
- **AND** the baseline condition row MUST have `normalized_latency` equal to `1.0`

#### Scenario: Ratio scan rows report normalized average read latency

- **WHEN** ratio-scan traces complete simulation successfully
- **THEN** the read-impact result MUST include one condition row for each ratio in `[0.1, 0.2, 0.4, 0.8]`
- **AND** each row MUST include the configured ratio, compute size `128`, raw average read latency, and normalized average latency relative to the baseline

#### Scenario: Request-size scan rows report normalized average read latency

- **WHEN** request-size-scan traces complete simulation successfully
- **THEN** the read-impact result MUST include one condition row for each compute request size in `[8, 32, 128, 512]`
- **AND** each row MUST include fixed ratio `0.2`, the configured compute request size, raw average read latency, and normalized average latency relative to the baseline

#### Scenario: Read mismatch fails explicitly

- **WHEN** the baseline and any compute-contention report do not contain the same read identities
- **THEN** the read-impact experiment MUST fail with an explicit mismatch error instead of producing a partial condition result

### Requirement: Read-impact experiment enforces CMT-hit reads

The read-impact experiment SHALL ensure every measured `read` request in the baseline trace and every compute-contention scan trace is served through CMT-hit mapping resolution. The workflow MUST generate or validate a read-impact precondition input containing every LPA touched by the measured reads, MUST pass that same precondition input to every simulator run, and MUST reject any read set whose touched LPA count cannot fit within the preconditioning CMT warm capacity. Compared reads MUST NOT trigger `mapping_read` resolution in any run.

#### Scenario: Generated default reads fit in CMT warm set

- **WHEN** the read-impact experiment generates default read commands from precondition data
- **THEN** the workflow MUST write a read-impact precondition input containing the LPAs touched by those reads
- **AND** the baseline simulator run and every compute-contention simulator run MUST use that same precondition input

#### Scenario: Custom read commands are validated before simulation

- **WHEN** the read-impact experiment receives custom read commands
- **THEN** it MUST verify that every sector touched by those reads is valid in the source precondition data
- **AND** it MUST reject the read set before simulation if any touched LPA is missing, invalid for the requested sector range, would exceed the CMT warm capacity, or is not a page-aligned page read

#### Scenario: Mapping-read report fails the experiment

- **WHEN** the baseline report or any compute-contention report contains a compared `READ` request whose mapping-resolution counts include `mapping_read > 0`
- **THEN** the read-impact experiment MUST fail with an explicit CMT-hit validation error instead of writing successful scan results

#### Scenario: All compared reads are CMT hits

- **WHEN** the baseline simulation and every compute-contention simulation complete and every compared `READ` request reports mapping-resolution counts with `cmt_hit` equal to the total mapping lookups and `mapping_read` equal to `0`
- **THEN** the read-impact output MUST include one condition-level result row for the baseline and one condition-level result row for each configured scan condition

## ADDED Requirements

### Requirement: Read-impact experiment emits grouped normalized bar chart

The read-impact experiment SHALL write one grouped bar-chart artifact for normalized average read latency. The chart MUST contain three x-axis groups in this order: baseline, insertion-ratio scan, and request-size scan. The baseline group MUST contain a bar with normalized latency `1.0` and use the default blue color. The insertion-ratio scan group MUST use ratio labels on the x-axis and orange bars. The request-size scan group MUST use compute request size labels on the x-axis and purple bars. Gaps between bars in the same group MUST be smaller than gaps between different groups. The chart MUST label each group below the x-axis and MUST format data labels with exactly two decimal places.

#### Scenario: Grouped chart contains the requested groups and colors

- **WHEN** the read-impact experiment completes successfully
- **THEN** the output root MUST contain a grouped normalized-latency bar chart
- **AND** the chart MUST contain a baseline group, an insertion-ratio scan group, and a request-size scan group in that order
- **AND** the baseline, insertion-ratio, and request-size bars MUST use blue, orange, and purple fills respectively

#### Scenario: Chart labels normalized values with two decimals

- **WHEN** the grouped read-impact chart is written
- **THEN** every bar value label MUST be formatted with exactly two decimal places
- **AND** the y-axis label MUST indicate normalized latency
