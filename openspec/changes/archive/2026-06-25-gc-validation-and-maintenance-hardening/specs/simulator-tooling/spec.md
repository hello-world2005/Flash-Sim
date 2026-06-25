## MODIFIED Requirements

### Requirement: GC pressure matrix runner covers every maintained variant

`python -m flash_sim.gc_pressure_matrix` SHALL execute all maintained timing, working-set, overwrite, low-invalid, post-flush, and in-flight-GC variants, plus auxiliary GC regressions. Each trace MUST have a configurable timeout and an independent machine-readable result.

#### Scenario: One failed or timed-out trace does not hide later results

- **WHEN** a trace raises an exception, remains incomplete, or exceeds its timeout
- **THEN** the runner MUST record that result and continue with the remaining matrix

#### Scenario: Summary checks maintenance conservation

- **WHEN** a trace completes
- **THEN** its result MUST include request status, GC/static-WL starts, relocated and erased pages, host and physical writes, waiting/cache residue, write amplification, and detected correctness issues

#### Scenario: Coalescing warning is not a correctness failure

- **WHEN** write amplification is below `1.0` but all reported counters remain internally consistent
- **THEN** the runner MUST retain a workload/coalescing warning without classifying the trace as failed

#### Scenario: Static WL has a real event-path regression

- **WHEN** the automated suite validates static wear leveling
- **THEN** it MUST complete one relocation through real Engine, TSU, and PHY instances and verify mapping, media data, erase state, barriers, and maintenance counters
