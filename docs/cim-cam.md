# CIM/CAM timing model

Flash-Sim does not calculate search matches, GEMV values, analog bit-line currents, or ADC codes. SEARCH and COMPUTE reuse the event-driven request, TSU, PHY, ONFI, and completion paths and model only validation, resource-compatible waves, fixed array execution time, and data-transfer time.

## Geometry and address unit

The static region uses the configured ordinary flash geometry:

```text
channel / static chip / die / plane / block / SL / SSL
```

`sl_per_block` and `ssl_per_sl` are shared configuration values; there is no separate static-area SL geometry. One static LHA maps to one SSL-granularity operation unit, so SEARCH, COMPUTE, and STATIC_WRITE each create one transaction per addressed `(block, SL, SSL)`.

The default CIM-facing geometry is:

```text
WL_PER_STRING                  = 128
BL_PER_PLANE                   = 262144
SEARCH_INPUT_BITS_PER_WL       = 1
SEARCH_MATCH_BITS_PER_BL       = 1
COMPUTE_INPUT_BITS_PER_SL      = 8
COMPUTE_ACCUMULATOR_BITS       = 8
COMPUTE_MAX_PARALLEL_SL        = 256
```

`selected_wl` exists only on a COMPUTE source request and must satisfy `0 <= selected_wl < WL_PER_STRING`.

## SEARCH waves

Different dies are independent. Within one die, a SEARCH wave contains one source request and at most one SSL transaction per plane. Different planes use the same request's WL keyword but produce independent BL match vectors. Plane results are logically concatenated during data-out; they are not electrically ORed.

Transactions that do not fit remain queued. Every subsequent wave repeats command transfer, keyword data-in, fixed `T_SEARCH`, and match data-out.

For one die wave:

```text
input_bytes = ceil(WL_PER_STRING * SEARCH_INPUT_BITS_PER_WL / 8)

output_bytes = participating_plane_count
             * ceil(BL_PER_PLANE * SEARCH_MATCH_BITS_PER_BL / 8)
```

With defaults, input is 16 B per wave and output is 32 KiB per participating plane.

## COMPUTE waves

Different dies may use different requests and selected WLs. The initial implementation conservatively requires all transactions in one die wave to have the same source-request identity and `selected_wl`.

Within each plane:

- at most `COMPUTE_MAX_PARALLEL_SL` transactions may participate;
- at most one SSL may be selected from each `(block, SL)`;
- different SLs in the same block may participate together;
- different SLs in different blocks may participate together.

Thus a COMPUTE transaction remains SSL-granular, while the conflict and input-counting unit is an active SL. Same-SL SSLs are split into later waves, and every wave repeats input transfer, fixed `T_COMPUTE`, and output transfer.

For one die wave:

```text
input_bytes = ceil(active_transaction_count
                 * COMPUTE_INPUT_BITS_PER_SL / 8)

output_bytes = participating_plane_count
              * ceil(BL_PER_PLANE * COMPUTE_ACCUMULATOR_BITS / 8)
```

The output model follows a multibit-current CIM interpretation: each BL is digitized into one configurable-width ADC/accumulator result. With 262,144 BLs and 8-bit quantization, one participating plane returns 256 KiB. This differs from an ordinary binary NAND page read, where one bit per BL yields 32 KiB.

This version does not implement NASiC-style simultaneous multi-SSL thermometer encoding within one SL. Supporting that organization requires a separate mapping and scheduling mode.

## ONFI and array timing

SEARCH and COMPUTE retain the existing ONFI channel priority, preemption, and resume behavior. Their input and output payload byte counts are frozen in each channel-transfer task. `T_SEARCH` and `T_COMPUTE` remain fixed array delays; configuration widths affect transfer time, not analog execution time.

Ordinary READ, WRITE, ERASE, mapping, and GC payload and scheduling behavior are unchanged.

## Deferred scheduling extension

Because different planes have independent BL domains, a future implementation may allow different COMPUTE source requests on different planes of one die when their `selected_wl` values match. The current conservative one-request-per-die-wave rule remains intentional and is tracked in `my_todo.md`.
