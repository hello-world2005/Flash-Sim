# External Trace Validation Analysis

- Profile: `flashsim-event-small-finite-cmt-aligned`
- Case: `read_only_1k_cmt_aligned`
- Status: `PASS`
- Main request count: expected=1000, Flash-Sim=1000
- MQSim trace request count: expected=2000 (warmup=1000), MQSim=2000, serviced=2000
- Main read/write requests: expected=1000/0, Flash-Sim=1000/0, MQSim trace expected=1000/1000, MQSim=1000/1000

## Trace Normalization

- Source Flash trace: `validation/mqsim_flash/traces/read_only/read_only_1k_64b_flashsim.json`
- Source MQSim trace: `validation/mqsim_flash/traces/read_only/read_only_1k_64b_mqsim.trace`
- Source requests: 1000
- Normalized requests: 1000
- Original page operations: 1000
- Unique normalized pages: 1000
- Address mode: `raw`
- Compact unique source pages: None
- Logical page limit: 182844
- Requests split: 0 (none)
- Flash-Sim precondition: {'mode': 'read-pages', 'page_count': 1000, 'min_lpa': 0, 'max_lpa': 999}
- Read-before-write in normalized main trace: {'request_count': 1000, 'page_ops': 1000}
- MQSim preconditioning: False, initial occupancy=0
- MQSim warmup prefix: enabled=True, requests=1000, gap_ns=250000, main_time_shift_ns=270000000

## Timing Snapshot

| Metric | Flash-Sim (us) | MQSim (us) |
| --- | ---: | ---: |
| Overall host/device avg | 12.06 | 130.00 |
| Read host/transaction avg | 12.06 | 6.00 |
| Write host/transaction avg | 0.00 | 252.00 |
| Read media service avg | 6.86 | 6.00 |
| Write media service avg | 0.00 | 251.00 |
| Flash-Sim read host p95 | 12.40 | n/a |
| Flash-Sim read host p99 | 12.40 | n/a |
| Flash-Sim write host p95 | 0.00 | n/a |
| Flash-Sim write host p99 | 0.00 | n/a |

## Maintenance And Mapping Diagnostics

- Flash-Sim maintenance: {'gc_count': 0, 'static_wl_count': 0, 'gc_relocated_pages': 0, 'gc_erased_blocks': 0, 'host_write_pages': 0, 'physical_user_write_pages': 0, 'physical_gc_write_pages': 0, 'min_free_pool': 0, 'max_wear_skew': 0, 'current_waiting_writes': 0, 'max_waiting_writes': 0, 'backpressure_enqueued': 0, 'backpressure_retried': 0, 'backpressure_wait_time': 0, 'precondition': {'mode': 'manual', 'source': '$FLASHSIM_ROOT/validation/mqsim_flash/results/read_only_cmt/flashsim-event-small-finite-cmt-aligned/read_only_1k_cmt_aligned/validation_flashsim-event-small-finite-cmt-aligned_read_only_1k_cmt_aligned_precondition.json', 'input_pages': 1000, 'actual_pages': 1000, 'actual_fill_ratio': 0.005517302259887006, 'preconditionable_pages': 181248, 'dropped_pages': 0, 'non_empty_planes': 384, 'plane_actual_min': 2, 'plane_actual_max': 3, 'cmt_warm_pages': 64}, 'planes': {}, 'write_amplification': 0.0}
- MQSim FTL GC/WL: gc=0, wl=0, mapping_reads=124
- MQSim effective page commands: reads=1124, user_reads=1000, programs=1000, erases=0

## Interpretation

The hard correctness gates passed: both simulators accepted and completed the same normalized full-page request stream, and Flash-Sim reported no host-visible request errors.

Latency is diagnostic for this external run. The input is full-page aligned, but address normalization and preconditioning policy still make this a controlled replay rather than a vendor-calibrated SSD performance result.

When an MQSim warmup prefix is enabled, MQSim aggregate latency and maintenance counters include the warmup phase; main-phase latency should not be directly compared until MQSim reports are split by phase.

## Issues

- None

## Notes

- MQSim trace includes a preconditioning warmup prefix: warmup_requests=1000, main_requests=1000, trace_requests=2000, main_time_shift_ns=270000000. MQSim generated/serviced gates use the full trace count; Flash-Sim request gates use the main trace count.
- Direct MQSim latency/GC comparison is disabled for this run because MQSim aggregate XML metrics include the warmup prefix. Use the metrics as diagnostics only unless a main-phase-only MQSim report is added.
- Latency equality is not asserted because Flash-Sim can complete writes at controller-cache acceptance while persistence is reported separately.
- MQSim XML latency fields are reported in microseconds; the HTML report converts them to nanoseconds for the diagnostic latency snapshot.
- External validation trace is normalized before replay: address_mode=raw, logical_page_limit=182844, precondition=read-pages, source_requests=1000, normalized_requests=1000.
