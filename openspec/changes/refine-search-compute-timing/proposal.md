## Why

SEARCH and COMPUTE currently reuse page-like payload sizing and permissive static scheduling, so their ONFI latency and parallel waves do not reflect the WL/SL/SSL/BL organization being modeled. The simulator needs an explicit timing-only CIM contract before further search/compute experiments are meaningful.

## What Changes

- Add configurable WL count, BL count, search input/match widths, compute SL-input width, and compute ADC/accumulator width.
- Add `selected_wl` to COMPUTE requests, validate its range, and keep SEARCH/COMPUTE/STATIC_WRITE transaction addressing at SSL granularity.
- Constrain a COMPUTE die wave to one source request and one selected WL, with at most one SSL selected from each SL and a configurable per-plane active-SL limit.
- Constrain a SEARCH die wave to one source request and at most one SSL transaction per plane; different dies remain independent scheduling domains.
- Model SEARCH and COMPUTE ONFI data-in and data-out with distinct directional payload formulas. COMPUTE produces one ADC-quantized result per BL, so the default 8-bit result width yields 256 KiB per participating plane for 262,144 BLs.
- Preserve fixed array execution times and existing ONFI arbitration; excess work is split into waves and each wave retransmits its input.
- Propagate event-engine ONFI timing, CIM payload geometry, WL validation, and COMPUTE parallel-limit overrides from `FlashConfig` into HIL, TSU, and PHY without replacing the compact runtime address geometry.
- Update simulator specifications, design documentation, and automated tests for request validation, wave scheduling, and directional ONFI payloads.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `default-flash-configuration`: Add deterministic, validated, round-trippable search/compute geometry and bit-width parameters.
- `host-device-request-flow`: Carry and validate COMPUTE `selected_wl` while preserving SSL-granularity static segmentation.
- `ftl-scheduling-and-media-model`: Define SEARCH/COMPUTE die-wave selection and per-plane parallel limits.
- `onfi-channel-transfer-model`: Derive SEARCH/COMPUTE directional payload sizes from WL, SL-transaction, BL, and result-width configuration.

## Impact

In scope are `flash_sim/config.py`, `flash_sim/common.py`, `flash_sim/parser.py`, `flash_sim/engine.py`, `flash_sim/HIL.py`, the SEARCH/COMPUTE scheduling functions in `flash_sim/FTL.py`, the payload and transfer-task logic in `flash_sim/PHY.py`, affected engine trace fixtures and generators, OpenSpec requirements, CIM design documentation, and focused plus regression tests.

Out of scope are functional search/GEMV value computation, ADC circuit delay modeling beyond the existing fixed `T_COMPUTE`, ordinary READ/WRITE/GC scheduling changes, and cross-request COMPUTE parallelism within one die.

Automated tests SHALL cover: invalid or missing `selected_wl`; two SSLs under the same SL splitting into separate COMPUTE waves; different SLs in one block sharing a wave; die-level request/WL compatibility; SEARCH one-transaction-per-plane waves; directional ONFI payload sizing, including 8-bit COMPUTE output of 256 KiB per participating plane; runtime configuration propagation; full-chip peak concurrency; and COMPUTE result-transfer preemption/resume.
