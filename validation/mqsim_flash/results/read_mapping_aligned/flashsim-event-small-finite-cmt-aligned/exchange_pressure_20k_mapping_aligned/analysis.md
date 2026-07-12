# Exchange peak-pressure 20k: MQSim-aligned GMT/CMT result

## Mapping fixes

- Clean CMT victims are discarded without a mapping-page write and without entering GMT.
- Dirty CMT victims enter GMT only as temporary departing/writeback entries.
- Dirty peer entries in the same MVPN are included in the mapping write but remain resident and become clean.
- GMT departing entries are removed when the mapping write completes.
- Concurrent host misses to the same MVPN share one in-flight mapping read.

The physical mapping table remains in NAND `PageType.MAPPING` pages addressed by GTD. GMT is not a second unlimited mapping cache.

## Read latency

| Simulator | Average | p50 | p95 | p99 | Min | Max |
|---|---:|---:|---:|---:|---:|---:|
| Flash-Sim, fixed | 16,154.495 ns | 14,748 ns | 26,346 ns | 33,912 ns | 7,556 ns | 95,934 ns |
| MQSim, fixed | 16,148.747 ns | 14,728 ns | 26,248 ns | 33,736 ns | 7,546 ns | 105,988 ns |

Before the mapping fix Flash-Sim averaged 68,686.072 ns and reached 90,379,874 ns. The fix removes the millisecond mapping stalls and reduces average latency by 76.48%.

## Aligned mapping result

- Flash-Sim mapping resolution: 78,488 mapping misses, 1,776 CMT hits, 0 GMT hits.
- Flash-Sim physical mapping-page reads: 20,131.
- MQSim read CMT misses: 78,508 and CMT hits: 1,756; misses split into 20,131 physical reads and 58,377 requests joined to an arriving MVPN read.
- MQSim physical mapping-page reads during the measured read phase: 20,131.
- Flash-Sim average Mapping field: 5,352.757 ns; maximum 12,254 ns.

The logical CMT hit/miss counts and physical mapping-page read counts are aligned. Flash-Sim is 5.748 ns slower on average; p50 differs by 20 ns, p95 by 98 ns, and p99 by 176 ns. These residuals are consistent with the small ONFI/PCIe timing differences already identified, rather than a mapping-path discrepancy.

Both runs completed all 20,000 reads successfully with zero GC, WL, and erase operations.

## MQSim branch-counter diagnosis

MQSim was instrumented so `request_mapping_entry()` reports read-only CMT-miss branches. The same 20k run produced:

| MQSim read-miss branch | Count | New physical read? | Current-request delay |
|---|---:|---|---|
| `NO_MPPN` | 0 | No | Immediate mapping creation |
| `Arriving_MVPN` | 58,377 | No new read | Waits for the already in-flight mapping read |
| `Departing_MVPN` | 0 | No | Immediate copy from in-memory GlobalMappingTable |
| `Physical_Read` | 20,131 | Yes | NAND/ONFI/TSU mapping-read latency |

The original MQSim run placed 1,362 mapping writes into TSU queues but dequeued none. Two defects caused this: `service_write_transaction()` never selected `MappingWriteTRQueue`, and mapping read-modify-write transactions lacked the reverse `readTR->RelatedWrite` dependency needed to clear `writeTR->RelatedRead`. After fixing both, all 1,362 mapping writes are dequeued and completed, Departing state clears, and formal reads execute the expected 20,131 physical mapping reads.
