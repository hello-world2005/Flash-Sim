# default-flash-configuration Specification

## Purpose
TBD - created by archiving change fix-pytest-regressions. Update Purpose after archive.
## Requirements
### Requirement: Default flash geometry baseline is consistent
The repository SHALL expose a single default geometry baseline for direct `FlashGeometry()` construction and for the default `FlashConfig().geometry` path. That baseline MUST match the documented 3D NAND configuration of `layers_per_block=128`, `sub_blocks_per_block=8`, `blocks_per_plane=1024`, `planes_per_die=2`, and `dies=1`.

#### Scenario: Direct geometry construction uses documented defaults
- **WHEN** a caller instantiates `FlashGeometry()` without overrides
- **THEN** the resulting object MUST report `layers_per_block=128`, `sub_blocks_per_block=8`, `blocks_per_plane=1024`, `planes_per_die=2`, and `dies=1`

#### Scenario: Default FlashConfig exposes the same geometry baseline
- **WHEN** a caller instantiates `FlashConfig()` without overrides
- **THEN** `FlashConfig().geometry` MUST expose the same default geometry values as a direct `FlashGeometry()` construction

### Requirement: Default configuration constructors and serializers stay aligned
`FlashConfig.from_dict({})`, `FlashConfig.to_dict()`, and geometry-facing tooling SHALL use the same default flash baseline as direct constructors, and MUST NOT silently substitute a different debugging geometry.

#### Scenario: Empty configuration input preserves the shared defaults
- **WHEN** a caller builds a config with `FlashConfig.from_dict({})`
- **THEN** the resulting geometry MUST match the default baseline used by `FlashConfig()` and `FlashGeometry()`

#### Scenario: Default config round-trip preserves geometry defaults
- **WHEN** a caller serializes `FlashConfig()` with `to_dict()` and reconstructs it with `from_dict(...)`
- **THEN** the reconstructed geometry MUST match the original default geometry values exactly

