# External Trace Validation Analysis

- Profile: `flashsim-event-small`
- Case: `read_only_10k_ideal_mapping`
- Status: `PASS`
- Main request count: expected=10000, Flash-Sim=10000
- MQSim trace request count: expected=20000 (warmup=10000), MQSim=20000, serviced=20000
- Main read/write requests: expected=10000/0, Flash-Sim=10000/0, MQSim trace expected=10000/10000, MQSim=10000/10000

## Trace Normalization

- Source Flash trace: `validation/mqsim_flash/traces/read_only/read_only_10k_64b_flashsim.json`
- Source MQSim trace: `validation/mqsim_flash/traces/read_only/read_only_10k_64b_mqsim.trace`
- Source requests: 10000
- Normalized requests: 10000
- Original page operations: 10000
- Unique normalized pages: 10000
- Address mode: `raw`
- Compact unique source pages: None
- Logical page limit: 182844
- Requests split: 0 (none)
- Flash-Sim precondition: {'mode': 'read-pages', 'page_count': 10000, 'min_lpa': 0, 'max_lpa': 9999}
- Read-before-write in normalized main trace: {'request_count': 10000, 'page_ops': 10000}
- MQSim preconditioning: False, initial occupancy=0
- MQSim warmup prefix: enabled=True, requests=10000, gap_ns=250000, main_time_shift_ns=2520000000

## Timing Snapshot

| Metric | Flash-Sim (us) | MQSim (us) |
| --- | ---: | ---: |
| Overall host/device avg | 7.06 | 130.00 |
| Read host/transaction avg | 7.06 | 6.00 |
| Write host/transaction avg | 0.00 | 251.00 |
| Read media service avg | 6.86 | 6.00 |
| Write media service avg | 0.00 | 251.00 |
| Flash-Sim read host p95 | 7.06 | n/a |
| Flash-Sim read host p99 | 7.06 | n/a |
| Flash-Sim write host p95 | 0.00 | n/a |
| Flash-Sim write host p99 | 0.00 | n/a |

## Maintenance And Mapping Diagnostics

- Flash-Sim maintenance: {'gc_count': 0, 'static_wl_count': 0, 'gc_relocated_pages': 0, 'gc_erased_blocks': 0, 'host_write_pages': 0, 'physical_user_write_pages': 0, 'physical_gc_write_pages': 0, 'min_free_pool': 0, 'max_wear_skew': 0, 'current_waiting_writes': 0, 'max_waiting_writes': 0, 'backpressure_enqueued': 0, 'backpressure_retried': 0, 'backpressure_wait_time': 0, 'precondition': {'mode': 'manual', 'source': '$FLASHSIM_ROOT/validation/mqsim_flash/results/read_only_ideal/flashsim-event-small/read_only_10k_ideal_mapping/validation_flashsim-event-small_read_only_10k_ideal_mapping_precondition.json', 'input_pages': 10000, 'actual_pages': 10000, 'actual_fill_ratio': 0.05517302259887006, 'preconditionable_pages': 181248, 'dropped_pages': 0, 'non_empty_planes': 384, 'plane_actual_min': 26, 'plane_actual_max': 27, 'cmt_warm_pages': 10000}, 'planes': {}, 'write_amplification': 0.0}
- MQSim FTL GC/WL: gc=0, wl=0, mapping_reads=0
- MQSim effective page commands: reads=10000, user_reads=10000, programs=10000, erases=0

## Interpretation

The hard correctness gates passed: both simulators accepted and completed the same normalized full-page request stream, and Flash-Sim reported no host-visible request errors.

Latency is diagnostic for this external run. The input is full-page aligned, but address normalization and preconditioning policy still make this a controlled replay rather than a vendor-calibrated SSD performance result.

When an MQSim warmup prefix is enabled, MQSim aggregate latency and maintenance counters include the warmup phase; main-phase latency should not be directly compared until MQSim reports are split by phase.

## Issues

- None

## Notes

- MQSim trace includes a preconditioning warmup prefix: warmup_requests=10000, main_requests=10000, trace_requests=20000, main_time_shift_ns=2520000000. MQSim generated/serviced gates use the full trace count; Flash-Sim request gates use the main trace count.
- Direct MQSim latency/GC comparison is disabled for this run because MQSim aggregate XML metrics include the warmup prefix. Use the metrics as diagnostics only unless a main-phase-only MQSim report is added.
- Latency equality is not asserted because Flash-Sim can complete writes at controller-cache acceptance while persistence is reported separately.
- MQSim XML latency fields are reported in microseconds; the HTML report converts them to nanoseconds for the diagnostic latency snapshot.
- External validation trace is normalized before replay: address_mode=raw, logical_page_limit=182844, precondition=read-pages, source_requests=10000, normalized_requests=10000.
