## Context

The event-driven path segments SEARCH, COMPUTE, and STATIC_WRITE requests into SSL-granularity static transactions. TSU has dedicated SEARCH/COMPUTE dispatch paths, but COMPUTE currently counts SSL transactions directly against a `COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL` allowance and neither path enforces request/WL wave compatibility. PHY sends data in and out for both operations using the same page-oriented payload helper.

The target remains a timing simulator: it does not calculate match vectors, GEMV values, analog currents, or ADC codes. It only models request validation, schedulable waves, fixed array latency, and transferred byte counts.

## Goals / Non-Goals

**Goals:**

- Represent the selected compute WL in the source request and validate it against configurable string geometry.
- Preserve SSL-granularity transactions while enforcing one active SSL per SL in a COMPUTE wave.
- Express WL sharing at die scope, BL independence at plane scope, and die independence at chip scope.
- Give SEARCH and COMPUTE separate, directional ONFI payload formulas.
- Model one configurable-width ADC result per COMPUTE BL; default 8-bit quantization produces 256 KiB per 262,144-BL plane.
- Keep static and ordinary geometry on the same `sl_per_block` configuration.

**Non-Goals:**

- Functional search, multiplication, accumulation, ADC, or concatenation values.
- Different source requests sharing one COMPUTE die wave.
- NASiC-style simultaneous multi-SSL encoding within one SL.
- Changes to normal READ/WRITE/ERASE scheduling or payload formulas.

## Decisions

### Configuration and request ownership

Add `wl_per_string=128`, `bl_per_plane=262144`, `search_input_bits_per_wl=1`, `search_match_bits_per_bl=1`, `compute_input_bits_per_sl=8`, and `compute_accumulator_bits=8` to `FlashGeometry`. Validate them as positive and preserve them through `FlashConfig.from_dict()` and `to_dict()`.

Only `Request` owns `selected_wl`; transactions consult `tr.source_req.selected_wl`. This avoids duplicated state and prevents request/transaction WL disagreement. Engine COMPUTE commands require `selected_wl`; HIL rejects a missing or out-of-range value before fetching input data.

### SSL-granularity addressing with SL-granularity conflicts

HIL keeps the existing one-static-LHA-to-one-transaction segmentation for SEARCH, COMPUTE, and STATIC_WRITE. A COMPUTE transaction therefore identifies one SSL. TSU decodes `(block, sl, ssl)` from the flattened static `sub_plane` address and uses `(plane, block, sl)` as the per-wave uniqueness key. Multiple SLs in the same block are compatible, but two SSLs belonging to the same SL are split across waves.

This is chosen over changing COMPUTE request size to SL units because it preserves the shared static address space and existing trace range semantics.

### Die-wave scheduling

For each die, SEARCH selects a seed request and dispatches at most one ready transaction from that request per plane. COMPUTE selects a seed request and selected WL, then dispatches only transactions with the identical source-request object and selected WL. Each plane admits at most `compute_max_parallel_sl` transactions and at most one SSL for each `(block, sl)`.

Different dies select their seeds independently. The initial implementation deliberately keeps one source request per die wave even though independent plane BL domains could support a future relaxation.

### Directional payloads

PHY freezes the computed payload byte count in each channel transfer task. This ensures preemption/resume uses the original duration and avoids recomputing payloads from mutable transaction state.

SEARCH data-in is one keyword per die wave:

`ceil(wl_per_string * search_input_bits_per_wl / 8)`.

SEARCH data-out is a concatenated match result per participating plane:

`plane_count * ceil(bl_per_plane * search_match_bits_per_bl / 8)`.

COMPUTE data-in contains one SL input per active transaction because the scheduler guarantees one selected SSL per active SL:

`ceil(transaction_count * compute_input_bits_per_sl / 8)`.

COMPUTE data-out contains one ADC-quantized result per BL per participating plane:

`plane_count * ceil(bl_per_plane * compute_accumulator_bits / 8)`.

The existing `T_SEARCH` and `T_COMPUTE` remain fixed array delays. Work beyond a wave's capacity stays queued; every later wave incurs a new command, input transfer, array delay, and output transfer.

### Event-runtime configuration propagation

`Engine(config=...)` passes `config.onfi` and the CIM-related fields from
`config.geometry` into the event-driven Device. HIL owns the configured
`wl_per_string` validation bound, TSU owns the configured
`compute_max_parallel_sl`, and PHY owns ONFI timing plus WL/BL and bit-width
payload parameters. The compact event-runtime address geometry (`channel`,
`chip`, `die`, `plane`, `block`, `sl`, and `ssl` counts) remains sourced from
`make_event_runtime_geometry()`; the public 1024-block geometry is not used to
resize the legacy event-driven storage arrays.

## Design Rationale

WL selection is modeled at die scope because normal multi-plane execution requires a common row/WL selection. BL outputs remain plane-local, so output payload scales with participating planes rather than transactions. The conservative source-request constraint prevents unrelated logical accumulations from being mixed while leaving a documented path to future per-plane request grouping.

Treating accumulator width as ADC bits per BL matches the selected multibit-current CIM interpretation: a normal binary page produces one bit per BL, while COMPUTE digitizes each BL into an eight-bit value. Consequently, the compute result is eight page-equivalents per plane rather than one fixed 32 KiB page.

## Risks / Trade-offs

- [Large COMPUTE output dominates ONFI time] → Keep ADC width configurable and make the formula explicit in reports/specs.
- [Existing COMPUTE traces lack `selected_wl`] → Update repository engine traces; external traces receive a clear schema/validation error.
- [Flattened `sub_plane` decoding is easy to misuse] → Centralize the block/SL/SSL decode helper and document its ordering.
- [Conservative die-wide request grouping leaves parallelism unused] → Track per-plane multi-request grouping in `my_todo.md`.
- [Scheduling tests can become coupled to global geometry] → Use focused TSU stubs and explicit flattened addresses, and retain end-to-end regression coverage for the integrated path.

## Migration Plan

1. Add and serialize configuration fields without changing ordinary page geometry.
2. Add `selected_wl` to request parsing/construction and update COMPUTE engine traces.
3. Replace SEARCH/COMPUTE selection rules while preserving queue and PHY interfaces.
4. Split PHY payload sizing by operation and direction.
5. Update specifications and CIM documentation.
6. Update affected fixtures/generators, add focused automated tests, and run targeted plus full regression suites.

Rollback consists of reverting this change; no persistent media format or external dependency is introduced.

## Open Questions

- Future work may allow different source requests on different planes of the same die when `selected_wl` is common.
- A later NASiC-specific mode may allow multiple SSLs of one SL to participate jointly instead of enforcing the conventional one-SSL-per-SL wave rule.
