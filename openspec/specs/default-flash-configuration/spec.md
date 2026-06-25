# default-flash-configuration Specification

## Purpose
TBD - created by archiving change fix-pytest-regressions. Update Purpose after archive.
## Requirements
### Requirement: Default flash geometry baseline is consistent
The repository SHALL expose a single default geometry baseline for direct `FlashGeometry()` construction and for the default `FlashConfig().geometry` path. That baseline MUST match the documented 3D NAND configuration of `layers_per_block=128`, `sub_blocks_per_block=4`, `blocks_per_plane=1024`, `planes_per_die=4`, and `dies=4`.

#### Scenario: Direct geometry construction uses documented defaults
- **WHEN** a caller instantiates `FlashGeometry()` without overrides
- **THEN** the resulting object MUST report `layers_per_block=128`, `sub_blocks_per_block=4`, `blocks_per_plane=1024`, `planes_per_die=4`, and `dies=4`

#### Scenario: Default FlashConfig exposes the same geometry baseline
- **WHEN** a caller instantiates `FlashConfig()` without overrides
- **THEN** `FlashConfig().geometry` MUST expose the same default geometry values as a direct `FlashGeometry()` construction

### Requirement: Default configuration constructors and serializers stay aligned
`FlashConfig.from_dict({})`, `FlashConfig.to_dict()`, and geometry-facing tooling SHALL use the same default flash baseline as direct constructors, and MUST NOT silently substitute a different debugging geometry. `FlashConfig` SHALL also expose event-driven runtime policy knobs for GC and write-backpressure behavior.

#### Scenario: Empty configuration input preserves the shared defaults
- **WHEN** a caller builds a config with `FlashConfig.from_dict({})`
- **THEN** the resulting geometry MUST match the default baseline used by `FlashConfig()` and `FlashGeometry()`

#### Scenario: Default config round-trip preserves geometry defaults
- **WHEN** a caller serializes `FlashConfig()` with `to_dict()` and reconstructs it with `from_dict(...)`
- **THEN** the reconstructed geometry MUST match the original default geometry values exactly

#### Scenario: Runtime GC/write-path policy has stable defaults
- **WHEN** a caller instantiates `FlashConfig()` without overrides
- **THEN** the runtime config MUST expose `gc_low_watermark=3`, `stop_servicing_writes_threshold=1`, `gc_victim_policy="greedy"`, and `static_wl_wear_gap_threshold=2`

#### Scenario: Runtime config round-trip preserves policy knobs
- **WHEN** a caller supplies runtime config values through `FlashConfig.from_dict(...)` and then serializes with `to_dict()`
- **THEN** the serialized `runtime` object MUST preserve `gc_low_watermark`, `stop_servicing_writes_threshold`, `gc_victim_policy`, and `static_wl_wear_gap_threshold`

#### Scenario: Unsupported GC victim policy fails explicitly
- **WHEN** a caller configures `gc_victim_policy` to a value other than `"greedy"`
- **THEN** config construction MUST fail explicitly instead of silently falling back to a different GC policy
