## ADDED Requirements

### Requirement: Standalone trace execution preserves caller-provided logical addresses
The standalone simulator tooling path formed by `parse_trace(...)`, `flash_sim.cli run`, and `FlashSimulator` SHALL preserve non-zero logical addresses and operation parameters from standalone traces. Commands expressed with standalone simulator fields such as `lba`, `address`, `block_address`, `wl_count`, `block_count`, or `layer` MUST be passed through or normalized without silently falling back to zero-valued addresses.

#### Scenario: Standalone trace keeps a non-zero read or write address
- **WHEN** a caller runs a standalone trace containing a `read` or `write` command with a non-zero logical address
- **THEN** the resulting `FlashSimulator` execution MUST target that same non-zero logical address instead of coercing it to `0`

#### Scenario: Standalone trace keeps operation-specific parameters
- **WHEN** a caller runs a standalone `search`, `compute`, or `erase` command that carries `wl_count`, `block_count`, `layer`, or `block_address`
- **THEN** the standalone tooling path MUST preserve those fields so the executed command reflects the caller-provided parameters

### Requirement: Standalone and engine trace schemas fail explicitly when mixed
The repository SHALL distinguish between standalone simulator traces and event-driven engine traces. If a caller routes an engine-style command set using fields such as `time`, `start_lha`, and `size` into the standalone simulator path, the tooling MUST reject it explicitly or require the caller to use the engine entrypoint, rather than silently interpreting the command as a valid standalone simulator request.

#### Scenario: Engine trace is rejected by the standalone runner
- **WHEN** `flash_sim.cli run` or another standalone simulator path receives a trace whose commands are expressed with engine-only fields such as `time`, `start_lha`, and `size`
- **THEN** the tooling MUST return a validation error or redirect to the dedicated engine path instead of executing a different logical address than the trace requested
