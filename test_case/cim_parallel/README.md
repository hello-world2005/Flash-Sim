# CIM parallelism traces

These traces use the default event-runtime geometry:

- static base LHA: `12,582,912`
- SSL units per SL: `4`
- SSL units per block: `2 * 4 = 8`
- blocks per plane in the event runtime: `64`
- SSL units per plane: `64 * 2 * 4 = 512`
- SSL units per die: `4 * 512 = 2,048`

Expected array-execution behavior:

| Trace | Expected result | Reason |
| --- | --- | --- |
| `compute_same_sl_serial.json` | serial | two SSLs belong to one `(block, SL)` |
| `compute_different_sl_parallel.json` | parallel | the range crosses from SL 0 to SL 1 in one block |
| `compute_cross_plane_parallel.json` | parallel | one request crosses the plane boundary |
| `compute_cross_die_independent.json` | parallel | different dies choose requests and WLs independently |
| `compute_same_die_requests_serial.json` | serial | one die wave uses one source request |
| `search_same_plane_serial.json` | serial | one SEARCH wave selects at most one SSL per plane |
| `search_cross_plane_parallel.json` | parallel | one request supplies one SSL in each of two planes |
| `search_same_die_requests_serial.json` | serial | one die wave uses one source request |
| `search_cross_die_independent.json` | parallel | different dies choose source requests independently |
| `compute_full_die.json` | 4 waves × 512 | one SSL from every SL in all four planes per wave |
| `search_full_die.json` | 512 waves × 4 | one SSL from every plane per wave |
| `compute_full_chip.json` | peak 2,048 | four dies overlap, each with a 512-transaction wave |
| `search_full_chip.json` | peak 16 | four dies overlap, each with a four-plane wave |

“Parallel” means that the raw `phy_array_exec` intervals overlap. “Serial” means
that the first array interval ends before the second starts.

The multi-request traces begin with an unrelated array-operation blocker. It keeps
the chip occupied long enough for both target requests to enter TSU, so the test
measures die-wave selection rather than PCIe request-arrival ordering. The blocker
uses SEARCH before COMPUTE targets and COMPUTE before SEARCH targets.

The full-die traces cover all `2,048` SSL addresses in die 0. For COMPUTE,
each plane contributes `64 blocks * 2 SL = 128` active transactions, so a die
wave contains `128 * 4 planes = 512` transactions and four SSL rounds are
required. SEARCH admits one transaction per plane, so it requires 512 waves of
four transactions.

The full-chip traces cover all `8,192` SSL addresses in the event runtime's
single static chip and verify that the four dies select waves independently.
