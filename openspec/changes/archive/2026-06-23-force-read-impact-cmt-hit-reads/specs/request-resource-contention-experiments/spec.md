## ADDED Requirements

### Requirement: Read-impact experiment enforces CMT-hit reads

The read-impact experiment SHALL ensure every measured `read` request in both the baseline trace and compute-contention trace is served through CMT-hit mapping resolution. The workflow MUST generate or validate a read-impact precondition input containing every LPA touched by the measured reads, MUST pass that same precondition input to both simulator runs, and MUST reject any read set whose touched LPA count cannot fit within the preconditioning CMT warm capacity. Compared reads MUST NOT trigger `mapping_read` resolution in either run.

#### Scenario: Generated default reads fit in CMT warm set

- **WHEN** the read-impact experiment generates default read commands from precondition data
- **THEN** the workflow MUST write a read-impact precondition input containing the LPAs touched by those reads
- **AND** both the baseline and compute-contention simulator runs MUST use that same precondition input

#### Scenario: Custom read commands are validated before simulation

- **WHEN** the read-impact experiment receives custom read commands
- **THEN** it MUST verify that every sector touched by those reads is valid in the source precondition data
- **AND** it MUST reject the read set before simulation if any touched LPA is missing, invalid for the requested sector range, or would exceed the CMT warm capacity

#### Scenario: Mapping-read report fails the experiment

- **WHEN** either the baseline report or compute-contention report contains a compared `READ` request whose mapping-resolution counts include `mapping_read > 0`
- **THEN** the read-impact experiment MUST fail with an explicit CMT-hit validation error instead of writing a successful comparison

#### Scenario: All compared reads are CMT hits

- **WHEN** both read-impact simulations complete and every compared `READ` request reports mapping-resolution counts with `cmt_hit` equal to the total mapping lookups and `mapping_read` equal to `0`
- **THEN** the comparison output MUST include one row per baseline read request using the existing read identity and completion-delta fields
