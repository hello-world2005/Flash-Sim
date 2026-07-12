# External Trace Validation Analysis

- Profile: `flashsim-event-small`
- Case: `write_stream`
- Status: `FAIL`
- Main request count: expected=1, Flash-Sim=1
- MQSim trace request count: expected=1 (warmup=0), MQSim=1, serviced=0
- Main read/write requests: expected=0/1, Flash-Sim=0/1, MQSim trace expected=0/1, MQSim=0/1

## Timing Snapshot

| Metric | Flash-Sim (us) | MQSim (us) |
| --- | ---: | ---: |
| Overall host/device avg | 253.21 | 0.00 |
| Read host/transaction avg | 0.00 | 0.00 |
| Write host/transaction avg | 253.21 | 251.00 |
| Read media service avg | 0.00 | 0.00 |
| Write media service avg | 251.90 | 251.00 |
| Flash-Sim read host p95 | 0.00 | n/a |
| Flash-Sim read host p99 | 0.00 | n/a |
| Flash-Sim write host p95 | 253.21 | n/a |
| Flash-Sim write host p99 | 253.21 | n/a |

## Maintenance And Mapping Diagnostics

- Flash-Sim maintenance: {'gc_count': 0, 'static_wl_count': 0, 'gc_relocated_pages': 0, 'gc_erased_blocks': 0, 'host_write_pages': 1, 'physical_user_write_pages': 1, 'physical_gc_write_pages': 0, 'min_free_pool': 0, 'max_wear_skew': 0, 'current_waiting_writes': 0, 'max_waiting_writes': 0, 'backpressure_enqueued': 0, 'backpressure_retried': 0, 'backpressure_wait_time': 0, 'precondition': {'mode': 'manual', 'source': '$FLASHSIM_ROOT/pre_data/precondition_data.json', 'input_pages': 50, 'actual_pages': 50, 'actual_fill_ratio': 0.0002758651129943503, 'preconditionable_pages': 181248, 'dropped_pages': 0, 'non_empty_planes': 46, 'plane_actual_min': 0, 'plane_actual_max': 2, 'cmt_warm_pages': 50}, 'planes': {}, 'write_amplification': 1.0}
- MQSim FTL GC/WL: gc=0, wl=0, mapping_reads=0
- MQSim effective page commands: reads=0, user_reads=0, programs=1, erases=0

## Interpretation

At least one hard correctness gate failed. Inspect the Issues section below before using latency differences as evidence.

Latency is diagnostic for this external run. The input is full-page aligned, but address normalization and preconditioning policy still make this a controlled replay rather than a vendor-calibrated SSD performance result.

When an MQSim warmup prefix is enabled, MQSim aggregate latency and maintenance counters include the warmup phase; main-phase latency should not be directly compared until MQSim reports are split by phase.

## Issues

- MQSim serviced request count 0 != expected trace count 1.

## Notes

- MQSim did not service every generated request; latency and maintenance metrics from this run are treated as diagnostics, not as cross-simulator comparison evidence.
- Skipped MQSim media-program gate because MQSim did not complete the expected trace.
- Latency equality is not asserted because Flash-Sim can complete writes at controller-cache acceptance while persistence is reported separately.
- MQSim XML latency fields are reported in microseconds; the HTML report converts them to nanoseconds for the diagnostic latency snapshot.
- Generated validation traces avoid partial-sector bitmaps; bitmap/partial-page semantics should be a later validation layer.
