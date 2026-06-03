## ADDED Requirements

### Requirement: Preconditioning callers provide AMU-backed mapping context
`Block_Manager.preconditioning(...)` SHALL execute with a valid `Address_Mapping_Unit` and `PHY` so it can materialize user-page placement, mapping pages, GTD entries, and any warmed CMT state consistently with the runtime mapping model. Full-engine startup MAY satisfy this contract through injected dependencies, but direct callers and unit-test fixtures MUST provide equivalent mapping context explicitly.

#### Scenario: Engine startup passes preconditioning mapping dependencies
- **WHEN** `Engine.Start_simulation(...)` invokes `block_manager.preconditioning(...)`
- **THEN** the call MUST provide the active runtime `PHY` and `Address_Mapping_Unit` instances needed to build mapping state

#### Scenario: Direct test fixture supplies explicit AMU context
- **WHEN** a unit test or standalone harness invokes `Block_Manager.preconditioning(...)` outside the full engine topology
- **THEN** the fixture MUST provide an explicit `Address_Mapping_Unit` or equivalent injected mapping context so preconditioning can complete successfully

#### Scenario: Missing AMU context fails fast with a clear error
- **WHEN** `Block_Manager.preconditioning(...)` is called without an explicit `AMU` and without an injected runtime `AMU` available from its owning topology
- **THEN** the method MUST fail with a clear caller-facing error instead of proceeding with partially initialized mapping state
