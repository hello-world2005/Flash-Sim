## ADDED Requirements

### Requirement: Search scheduling forms request-compatible die waves

TSU SHALL form each SEARCH die wave from one source-request object and SHALL select at most one ready SEARCH transaction per plane. Different dies MAY select different source requests in the same chip activation. Transactions that do not fit the current wave MUST remain queued for a later wave.

#### Scenario: Same die search requests split into waves

- **WHEN** one die has ready SEARCH transactions from different source requests
- **THEN** TSU MUST NOT dispatch those source requests in the same die wave

#### Scenario: Different dies choose independent search requests

- **WHEN** different dies have ready SEARCH transactions belonging to different requests
- **THEN** each die MAY dispatch its own request-compatible wave

#### Scenario: Search selects at most one transaction per plane

- **WHEN** a request has multiple ready SEARCH transactions for one plane
- **THEN** TSU MUST dispatch at most one of them in the current die wave and leave the remainder queued

### Requirement: Compute scheduling enforces die WL compatibility and one SSL per SL

TSU SHALL form each COMPUTE die wave from transactions whose `source_req` objects are identical and whose request-level `selected_wl` values are identical. Within each plane, the wave MUST contain at most `compute_max_parallel_sl` transactions and MUST contain at most one SSL transaction for each `(block, sl)`. Different SLs in the same block MAY participate together, and different dies MAY choose different source requests and selected WLs.

#### Scenario: Same SL SSLs split across waves

- **WHEN** two ready COMPUTE transactions address different SSLs belonging to the same block and SL
- **THEN** TSU MUST dispatch at most one in the current wave and leave the other queued

#### Scenario: Different SLs in one block share a wave

- **WHEN** two ready COMPUTE transactions address different SLs in the same block and otherwise satisfy die compatibility
- **THEN** TSU MAY dispatch both in the same wave

#### Scenario: Source request or WL mismatch splits a die wave

- **WHEN** ready COMPUTE transactions on one die have different source-request identities or different selected WLs
- **THEN** TSU MUST place them in different waves

#### Scenario: Different dies choose independent compute groups

- **WHEN** different dies have ready COMPUTE transactions with different requests or selected WLs
- **THEN** each die MAY dispatch its own compatible wave

#### Scenario: Per-plane active SL limit creates later waves

- **WHEN** one plane has more compatible ready SLs than `compute_max_parallel_sl`
- **THEN** TSU MUST dispatch no more than the configured limit and leave excess transactions queued

