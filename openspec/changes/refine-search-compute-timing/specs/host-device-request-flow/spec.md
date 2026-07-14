## ADDED Requirements

### Requirement: Compute requests identify and validate a selected WL

Event-driven COMPUTE commands SHALL carry a `selected_wl` integer that is stored only on the source `Request`. Before segmentation or input-data fetch, HIL MUST require `0 <= selected_wl < wl_per_string`; a missing, non-integer, or out-of-range value MUST complete the request with `ERROR` and MUST NOT submit transactions to FTL or PHY.

#### Scenario: Valid selected WL reaches compute transactions through the request

- **WHEN** a COMPUTE command supplies a `selected_wl` within the configured range
- **THEN** Engine MUST store it on the source request and every derived transaction MUST observe that value through `source_req`

#### Scenario: Missing selected WL is rejected

- **WHEN** a COMPUTE command omits `selected_wl`
- **THEN** parsing or request validation MUST reject the command before compute input is fetched

#### Scenario: Out-of-range selected WL is rejected

- **WHEN** a COMPUTE request selects a WL below zero or at least `wl_per_string`
- **THEN** HIL MUST complete it with `ERROR` without submitting any transaction to FTL or PHY

### Requirement: Static compute segmentation remains SSL-granular

HIL SHALL segment SEARCH, COMPUTE, and STATIC_WRITE ranges at the shared static-LHA unit, where each unit maps to one `(block, sl, ssl)` operation address. COMPUTE MUST NOT reinterpret `size` as an SL count or require an `ssl=0` alignment.

#### Scenario: Multiple SSLs under one SL produce separate transactions

- **WHEN** a COMPUTE range covers two static LHAs that map to different SSLs under the same SL
- **THEN** HIL MUST generate two distinct COMPUTE transactions and leave their wave compatibility to TSU

