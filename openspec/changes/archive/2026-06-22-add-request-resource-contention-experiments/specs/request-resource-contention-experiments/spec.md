## ADDED Requirements

### Requirement: Experiment tooling generates isolated compute and search size-scan traces

The repository SHALL provide experiment tooling that accepts a list of positive request sizes and generates one engine trace for every `(request_type, size)` pair where `request_type` is `compute` or `search`. Each generated trace MUST contain exactly one request, MUST issue that request alone, and MUST keep the request inside the static-address domain used by event-driven `compute` and `search` requests.

#### Scenario: Single-request trace is generated for each size

- **WHEN** the size-scan experiment is configured with sizes `[1, 2, 4]`
- **THEN** the tooling MUST generate six traces: one `compute` trace and one `search` trace for each configured size
- **AND** every generated trace MUST contain exactly one command with fields `type`, `time`, `start_lha`, and `size`

#### Scenario: Invalid scan sizes are rejected

- **WHEN** the size-scan experiment is configured with a zero or negative size
- **THEN** the tooling MUST reject the configuration before running the simulator

### Requirement: Size-scan experiment records raw and normalized latency results

The size-scan experiment SHALL run the event-driven simulator once for each generated single-request trace and SHALL collect request latency data from the generated request latency JSON report. The aggregate result MUST preserve raw `host_completion_time` and `total_latency` values for every `(request_type, size)` pair. The plotted latency value MUST be normalized per request type by dividing each raw `total_latency` by the maximum raw `total_latency` measured for that same request type; if the maximum is zero, all normalized values for that request type MUST be `0`.

#### Scenario: One simulation is run for each generated trace

- **WHEN** the size-scan experiment is configured with two request sizes
- **THEN** the tooling MUST invoke the simulator four times, once for each generated `compute` or `search` trace

#### Scenario: Latency normalization preserves raw data

- **WHEN** the size-scan experiment aggregates raw `compute` latencies `[10, 20]` and raw `search` latencies `[5, 10]`
- **THEN** the aggregate output MUST keep the raw latency values
- **AND** the normalized values MUST be `[0.5, 1.0]` for `compute` and `[0.5, 1.0]` for `search`

### Requirement: Size-scan experiment emits normalized bar charts

The size-scan experiment SHALL write a bar-chart artifact for `compute` and a bar-chart artifact for `search`. Each chart MUST use request `size` as the x-axis categories and normalized latency as the y-axis values. The chart artifact MUST be written under the experiment output root together with machine-readable aggregate data.

#### Scenario: Charts are written with size labels and normalized values

- **WHEN** the size-scan experiment completes successfully
- **THEN** the output root MUST contain machine-readable aggregate results
- **AND** it MUST contain a `compute` bar chart and a `search` bar chart whose bar labels correspond to the scanned sizes

### Requirement: Experiment tooling generates paired read-impact traces

The repository SHALL provide tooling to generate a baseline read trace and a compute-contention read trace. The read portions of both traces MUST be identical, including request count, order, `type`, `time`, `start_lha`, and `size`. The compute-contention trace MUST prepend exactly two `compute` requests before the read portion. After simulating the compute-contention trace, both prepended compute requests MUST have host completion times later than the first read request issue time.

#### Scenario: Read portions are identical

- **WHEN** the read-impact trace generator creates the baseline and compute-contention traces
- **THEN** filtering each trace to `read` requests MUST produce byte-for-byte identical read command arrays

#### Scenario: Contention trace prepends two compute requests

- **WHEN** the read-impact trace generator creates the compute-contention trace
- **THEN** the first two commands MUST be `compute` requests
- **AND** the remaining commands MUST match the baseline read trace exactly

#### Scenario: Prepended compute requests overlap the first read issue

- **WHEN** the read-impact experiment simulates the compute-contention trace
- **THEN** both prepended `compute` report entries MUST have `host_completion_time` greater than the first read request's issue time

### Requirement: Read-impact experiment compares read completion times

The read-impact experiment SHALL simulate both paired traces and compare read completion times using read request identity `(type, time, start_lha, size)`. The comparison output MUST include, for every matched read request, the baseline `host_completion_time`, contended `host_completion_time`, and completion-time delta. Non-read prepended requests MUST NOT be included as comparison rows.

#### Scenario: Matched reads report completion deltas

- **WHEN** both read-impact traces complete simulation successfully
- **THEN** the comparison output MUST include one row per baseline read request
- **AND** every row MUST include the matching read identity, baseline completion time, contended completion time, and contended-minus-baseline delta

#### Scenario: Read mismatch fails explicitly

- **WHEN** the baseline and compute-contention reports do not contain the same read identities
- **THEN** the read-impact experiment MUST fail with an explicit mismatch error instead of producing a partial comparison
