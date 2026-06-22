## ADDED Requirements

### Requirement: ONFI channel scheduler uses explicit transfer priorities

The PHY SHALL model each ONFI channel as a single shared transfer resource and SHALL select pending transfer tasks by the following priority order, highest first: command transfer, search/write/compute data-in, GC write data-in, mapping data-out, user data-out, search/compute data-out, and GC read data-out. The scheduler MUST apply this ordering whenever a channel becomes available or whenever a new transfer task is submitted to the channel scheduler.

#### Scenario: Command wins over pending data transfers

- **WHEN** a channel has pending command, user data-out, and GC read data-out tasks
- **THEN** the PHY MUST start the command transfer before either data-out transfer

#### Scenario: Mapping data-out wins over user and GC read data-out

- **WHEN** a channel has pending mapping data-out, user data-out, search/compute data-out, and GC read data-out tasks
- **THEN** the PHY MUST start mapping data-out first, user data-out second, search/compute data-out third, and GC read data-out last

#### Scenario: GC write data-in is lower priority than user or static data-in

- **WHEN** a channel has pending search/write/compute data-in and GC write data-in tasks
- **THEN** the PHY MUST start the search/write/compute data-in task before the GC write data-in task

### Requirement: Commands preempt active data-out transfers

When a command transfer is submitted to a channel whose active transfer is data-out, the PHY SHALL interrupt the active data-out transfer, mark its existing completion event as ignored, compute and store the remaining data-out duration, run the command transfer, and later resume the interrupted data-out by registering a new completion event for the remaining duration. Command transfers MUST NOT preempt active command or data-in transfers.

#### Scenario: Read command interrupts user data-out

- **WHEN** user data-out is active on a channel and a read command transfer becomes ready on the same channel before that data-out completion event fires
- **THEN** the PHY MUST mark the original data-out completion event as ignored
- **AND** the PHY MUST register and complete the read command transfer before resuming the remaining user data-out

#### Scenario: Stale ignored data-out event does not complete a transaction

- **WHEN** a previously scheduled data-out completion event has been marked ignored due to command preemption
- **THEN** executing that stale event MUST NOT mark its transaction complete, broadcast transaction-serviced callbacks, release chip data-out state, or emit duplicate latency intervals

#### Scenario: Data-in is not preempted by command

- **WHEN** data-in is active on a channel and a command transfer becomes ready on the same channel
- **THEN** the command transfer MUST wait until the active data-in transfer completes

### Requirement: Resumed data-out preserves remaining transfer duration

The PHY SHALL track the active data-out transfer's start time, scheduled finish time, elapsed duration, and remaining duration when preempted. The resumed transfer MUST run for exactly the remaining duration, and the transaction MUST complete only after the resumed transfer completion event fires.

#### Scenario: Data-out resumes with remaining time

- **WHEN** a data-out transfer with total duration `1000ns` is preempted after `300ns`
- **THEN** the resumed data-out transfer MUST register a completion event `700ns` after its resume time

#### Scenario: Multiple command preemptions accumulate remaining time correctly

- **WHEN** the same data-out transfer is preempted by commands more than once
- **THEN** each resume MUST use the latest remaining duration and the transaction MUST complete after the sum of all completed data-out segments equals the original data-out transfer duration

### Requirement: ONFI transfer durations use MQSim-style NVDDR2 timing

The simulator SHALL derive ONFI command, data-in, and data-out transfer durations from NVDDR2 timing parameters, channel width, plane count, and transferred payload bytes instead of using one fixed duration for all transfers. Data-in duration MUST follow a MQSim-style two-unit DDR calculation based on payload bytes, channel width, and `TwoUnitDataInTime`. Data-out duration MUST follow the corresponding calculation using `TwoUnitDataOutTime`. Read, program, and erase command transfer durations MUST follow MQSim-style plane-count formulas using the ONFI timing parameters `t_CS`, `t_WC`, `t_WB`, `t_RR`, `t_DBSY`, `t_ADL`, `t_WPST`, `t_WPSTH`, `t_CALS`, `t_RPRE`, `t_DQSRE`, `t_RHW`, and `t_CCS` as applicable.

#### Scenario: Payload size changes data-in duration

- **WHEN** two write-like data-in transfers use the same ONFI timing parameters and channel width but one transfer has twice the payload bytes of the other
- **THEN** the larger transfer MUST have a larger data-in duration derived from payload size

#### Scenario: Payload size changes data-out duration

- **WHEN** two read-like data-out transfers use the same ONFI timing parameters and channel width but one transfer has twice the payload bytes of the other
- **THEN** the larger transfer MUST have a larger data-out duration derived from payload size

#### Scenario: Plane count changes command duration

- **WHEN** read or program command transfers target different valid plane counts
- **THEN** the command transfer duration MUST use the corresponding plane-count formula rather than a constant command delay

#### Scenario: Non-zero payload has positive transfer duration

- **WHEN** a data-in or data-out transfer has a non-zero payload size
- **THEN** the computed ONFI transfer duration MUST be greater than zero

### Requirement: ONFI transfer configuration exposes deterministic defaults

The simulator SHALL provide deterministic default ONFI/NVDDR2 timing parameters and channel width. These defaults MUST be available to the PHY timing helpers without requiring every caller to pass an explicit configuration object. If the simulator configuration exposes ONFI timing overrides, the PHY MUST use the configured values for all subsequent transfer calculations in that simulation.

#### Scenario: Defaults are used when no override is provided

- **WHEN** a test or simulation constructs the event-driven PHY without explicit ONFI timing overrides
- **THEN** the PHY MUST use deterministic default channel width and NVDDR2 timing parameters

#### Scenario: Configured channel width affects transfer duration

- **WHEN** the same non-zero data transfer is simulated with a wider configured ONFI channel
- **THEN** the computed transfer duration MUST be lower than or equal to the duration computed with the narrower channel, all other timing parameters being equal

### Requirement: Latency reporting reflects preempted transfer segments

When ONFI transfers are preempted or split, request latency reports SHALL record only the intervals during which a request's transfer is actually active on the channel. A preempted data-out transfer MUST appear as multiple `phy_data_out` intervals separated by the interrupting command interval. The report MUST NOT count the preemption gap as data-out time for the interrupted request.

#### Scenario: Preempted data-out is reported as split intervals

- **WHEN** a user read data-out transfer is preempted by a command transfer and later resumed
- **THEN** the request latency report for that read MUST contain at least two `phy_data_out` intervals
- **AND** no `phy_data_out` interval for that read MUST overlap the command transfer interval

#### Scenario: Total reported data-out equals completed transfer segments

- **WHEN** a data-out transfer is split by one or more command preemptions
- **THEN** the reported `phy_data_out` duration for the affected request MUST equal the sum of its completed data-out transfer segments
