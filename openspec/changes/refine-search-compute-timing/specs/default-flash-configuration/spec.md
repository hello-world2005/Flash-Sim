## ADDED Requirements

### Requirement: Search and compute geometry is configurable and round-trippable

The simulator SHALL expose positive integer configuration values for `wl_per_string`, `bl_per_plane`, `search_input_bits_per_wl`, `search_match_bits_per_bl`, `compute_input_bits_per_sl`, and `compute_accumulator_bits`. The deterministic defaults MUST be `128`, `262144`, `1`, `1`, `8`, and `8` respectively. `FlashConfig.from_dict(...)` and `FlashConfig.to_dict()` MUST preserve these fields, and static addressing MUST continue to use the same `sl_per_block` and `ssl_per_sl` geometry as the rest of the simulator.

#### Scenario: Default CIM geometry is deterministic

- **WHEN** a caller constructs the default event-driven geometry
- **THEN** it MUST expose 128 WLs per string, 262,144 BLs per plane, one-bit SEARCH WL input and BL match widths, and eight-bit COMPUTE SL input and accumulator widths

#### Scenario: CIM configuration round-trip preserves overrides

- **WHEN** a caller supplies non-default search/compute geometry and bit widths through `FlashConfig.from_dict(...)` and serializes the result
- **THEN** all supplied values MUST be preserved by `to_dict()`

#### Scenario: Non-positive CIM configuration is rejected

- **WHEN** any search/compute geometry count or bit width is zero or negative
- **THEN** configuration construction MUST fail explicitly

### Requirement: Event engine applies CIM and ONFI overrides

The event-driven Engine SHALL apply `FlashConfig.onfi` to PHY transfers and
SHALL apply the configured WL count, BL count, SEARCH/COMPUTE bit widths, and
COMPUTE per-plane parallel-SL limit to HIL, TSU, and PHY. It MUST retain the
compact event-runtime structural address geometry rather than allocating the
public default 1024-block geometry.

#### Scenario: Configured CIM payload changes event-engine transfer duration

- **WHEN** an Engine is constructed with non-default ONFI channel width, BL count, or result width
- **THEN** its PHY payload bytes and transfer duration MUST reflect those values

#### Scenario: Configured WL bound changes request validation

- **WHEN** an Engine is constructed with a smaller `wl_per_string`
- **THEN** HIL MUST reject COMPUTE selections outside that configured range
