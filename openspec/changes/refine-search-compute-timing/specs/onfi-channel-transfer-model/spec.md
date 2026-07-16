## ADDED Requirements

### Requirement: Search ONFI transfers use directional CIM payloads

For each SEARCH die wave, PHY SHALL compute data-in bytes as `ceil(wl_per_string * search_input_bits_per_wl / 8)` exactly once for the wave. It SHALL compute data-out bytes as `participating_plane_count * ceil(bl_per_plane * search_match_bits_per_bl / 8)`, representing concatenated plane-local BL match results. Every later wave MUST incur a new input and output transfer.

#### Scenario: Default search wave payload

- **WHEN** one SEARCH wave uses the default 128 WLs, one input bit per WL, 262,144 BLs, and one match bit per BL across two planes
- **THEN** PHY MUST model 16 bytes of data-in and 65,536 bytes of data-out

#### Scenario: Search wave retransmits its keyword

- **WHEN** queued SEARCH transactions require two waves
- **THEN** each wave MUST independently incur the configured keyword data-in transfer

### Requirement: Compute ONFI transfers use active SL inputs and per-BL ADC outputs

For each COMPUTE die wave, PHY SHALL compute data-in bytes as `ceil(active_transaction_count * compute_input_bits_per_sl / 8)`, because TSU admits at most one selected SSL per active SL. It SHALL compute data-out bytes as `participating_plane_count * ceil(bl_per_plane * compute_accumulator_bits / 8)`, representing one configurable-width ADC/accumulator result per BL. Every later wave MUST incur a new input and output transfer.

#### Scenario: Default eight-bit compute output

- **WHEN** one COMPUTE wave uses 262,144 BLs and eight-bit accumulator results across one plane
- **THEN** PHY MUST model 262,144 bytes of data-out for that plane

#### Scenario: Compute input follows active transaction count

- **WHEN** a COMPUTE wave contains 32 SSL transactions selected from 32 distinct SLs and uses eight input bits per SL
- **THEN** PHY MUST model 32 bytes of data-in

#### Scenario: Compute wave retransmits input and output

- **WHEN** same-SL conflicts or the per-plane active-SL limit split a COMPUTE request into multiple waves
- **THEN** every wave MUST independently incur its data-in, fixed array execution, and data-out phases

### Requirement: Search and compute retain fixed array delays and channel arbitration

Directional CIM payload sizing SHALL affect ONFI transfer duration without changing the fixed `T_SEARCH` and `T_COMPUTE` array phases or the existing channel priority, preemption, and resume rules.

#### Scenario: Wider ADC result increases only transfer duration

- **WHEN** `compute_accumulator_bits` is increased while the selected transactions and fixed `T_COMPUTE` are unchanged
- **THEN** COMPUTE data-out duration MUST increase according to payload bytes while the array execution duration remains `T_COMPUTE`

