# Flash-Sim vs MQSim Validation

This directory contains a small correctness-validation harness for comparing
Flash-Sim's event-driven traditional-flash path with MQSim.

The first target is deliberately narrow:

- full-page aligned requests only
- trace-based workloads only
- no sector bitmap / partial-page semantics
- no compute/search/static-flash features
- no attempt to calibrate to a modern SSD yet

This keeps the first comparison focused on the shared flash-SSD semantics:
request ingestion, address unit conversion, request completion, basic timing
constants, and NAND command accounting.

## Local Assets And Dependency Boundary

The validation harness is intended to be reproducible from this Flash-Sim
checkout plus one MQSim checkout/binary. The expected MQSim location is the
sibling directory `../MQSim`, and both validation runners default to
`../MQSim/MQSim`. You can also pass an explicit MQSim binary to
`run_validation.py` with `--mqsim-bin`.

Everything else needed for the current Flash-Sim-side validation flow lives
under this repository:

- Fixed run-test traces:
  `validation/mqsim_flash/traces/run_test/`.
- Validation scripts:
  `validation/mqsim_flash/run_validation.py` and
  `validation/mqsim_flash/run_test_matrix_latest.py`.
- Targeted trace/precondition report helper:
  `validation/mqsim_flash/run_small64_30k_latency_result.py`.
- Generated reports and temporary inputs:
  `validation/mqsim_flash/out/`, `report/`, and top-level `test_result.md`.

The fixed run-test traces are the actual compact-normalized Exchange disk0
inputs used for the current 10k/30k matrix:

```text
validation/mqsim_flash/traces/run_test/exchange_disk0_page_10k_compact_flashsim.json
validation/mqsim_flash/traces/run_test/exchange_disk0_page_10k_compact_mqsim.trace
validation/mqsim_flash/traces/run_test/exchange_disk0_page_30k_compact_flashsim.json
validation/mqsim_flash/traces/run_test/exchange_disk0_page_30k_compact_mqsim.trace
validation/mqsim_flash/traces/run_test/manifest.json
```

The `10k` and `30k` labels refer to source Exchange request windows. During
full-page compact normalization, multi-page requests are split, so the replay
request counts are 10387 and 33241 respectively. The manifest records the
request counts and SHA-256 hashes.

## Recommended Environment

Run validation commands from the repository root:

```bash
cd /home/kkkkaa/文档/PKU/轮转/李萌/Flash-Sim-pre-modern
PY=/home/kkkkaa/文档/PKU/轮转/李萌/.venv/bin/python
```

The scripts also try to find this sibling virtualenv automatically, but using
`$PY` makes runs explicit and repeatable when sharing commands.

MQSim is optional for Flash-Sim-only checks, but comparison runs expect an
MQSim checkout at `../MQSim` and a binary at `../MQSim/MQSim`. The matrix
scripts use the existing binary and do not rebuild it. If MQSim is missing,
exits, or fails to produce a parseable XML report, the scripts still keep the
Flash-Sim result and record the MQSim case as failed or skipped.

If MQSim is not in the sibling `../MQSim` directory, set `MQSIM_ROOT` to the
MQSim checkout root before running the scripts:

```bash
export MQSIM_ROOT=/absolute/path/to/MQSim
$PY validation/mqsim_flash/run_test_matrix_latest.py
```

`MQSIM_ROOT` should point to the directory that contains the `MQSim` binary, not
to the binary file itself.

## Which Runner To Use

Use `run_validation.py` for focused correctness/debug cases:

```bash
$PY validation/mqsim_flash/run_validation.py --skip-build --timeout 300
$PY validation/mqsim_flash/run_validation.py --case gc_pressure --skip-build --timeout 300
```

Use `run_test_matrix_latest.py` for the current `run_test.md` matrix:

```bash
$PY validation/mqsim_flash/run_test_matrix_latest.py
```

This runs the fixed 10k and 30k compact-normalized traces, both `small64` and
`modern256` geometries, both `bypass` and `cache64` modes, and the configured
precondition percentages. It writes `test_result.md` plus per-case artifacts
under `validation/mqsim_flash/out/run_test_matrix_latest/<run_id>/`.

Use `run_small64_30k_latency_result.py` for targeted small64 trace/precondition
latency experiments without running the whole matrix:

```bash
$PY validation/mqsim_flash/run_small64_30k_latency_result.py \
  --trace-label 30k \
  --precondition 25 \
  --precondition 50 \
  --cache-mode bypass \
  --cache-mode cache64 \
  --result-path result_small64_30k.md \
  --out-root validation/mqsim_flash/out/small64_30k_latency_result
```

The targeted helper accepts `--trace-label 10k|30k|50k`, repeated
`--precondition N`, and repeated `--cache-mode bypass|cache64`. The fixed 10k
and 30k traces are fully local under `traces/run_test`; the 50k label uses
`validation/mqsim_flash/public_traces/exchange/` if those generated files are
present.

## Current Trace Semantics

Flash-Sim trace addresses use the simulator host sector unit, which is 64 B.
The current page size is 4 KiB, so one page is 64 Flash-Sim host sectors. The
fixed 10k and 30k run-test traces are already compact-normalized and page
aligned, so they should be replayed with `raw` external address mode.

For the run-test matrix:

- `small64` uses 8 pages/block and 64 blocks/plane. With 8 channels, 3 data
  chips/channel, 4 dies/chip, and 4 planes/die, this is 196608 data pages, or
  768 MiB at 4 KiB/page. Flash-Sim also has one static chip/channel, so total
  physical storage including static chips is 1 GiB.
- `modern256` uses the same compact event profile and 8 pages/block, but 256
  blocks/plane. This is 786432 data pages, or 3 GiB at 4 KiB/page. Total
  physical storage including static chips is 4 GiB.
- `bypass` means Flash-Sim `cache_bypass=true` and MQSim
  `Device_Level_Data_Caching_Mode=TURNED_OFF`.
- `cache64` means Flash-Sim `data_cache_capacity=65536` and MQSim
  `WRITE_CACHE` with `Data_Cache_Capacity=65536`.
- Flash-Sim preconditioning uses runtime `precondition_fill_ratio` with
  `capacity-fill`; MQSim uses built-in preconditioning with
  `Enabled_Preconditioning=true` and the matching
  `Initial_Occupancy_Percentage`.
- The current matrix writes Flash-Sim GC knobs including
  `gc_exec_threshold=0.05`, `gc_victim_policy=d-choices`, and
  `gc_d_choices=6`.

Important generated files:

- `test_result.md`: latest full matrix report.
- `result_small64_30k.md` or a custom `--result-path`: targeted latency report.
- `validation/mqsim_flash/out/.../summary.json`: structured per-run summary.
- `validation/mqsim_flash/out/.../flashsim_config.json`: generated Flash-Sim
  runtime config for a case.
- `validation/mqsim_flash/out/.../mqsim_ssdconfig.xml` and
  `mqsim_workload.xml`: generated MQSim inputs.
- `report/*_request_latency.json`: Flash-Sim request-level latency reports.

## FAST'18 Workload Sources

The MQSim FAST'18 paper uses three broad workload groups:

- Validation against real SSDs: `tpcc`, `tpce`, and `exchange` traces from the
  Microsoft Enterprise Traces collection.
- Synthetic contention studies: write-cache contention, CMT contention, and
  backend contention experiments.
- QueueFetchSize application studies: filesystem/mail/web/io-style application
  workloads generated with full-system workloads and then replayed by MQSim.

What is present in the sibling MQSim checkout used by this harness:

- `MQSim/fast18/data-cache-contention`
- `MQSim/fast18/backend-contention`
- `MQSim/fast18/queue-fetch-size`
- `MQSim/traces/tpcc-small.trace`
- `MQSim/traces/wsrch-small.trace`

The full paper-validation traces (`tpcc`, `tpce`, `exchange`) are not all
present as full datasets in this checkout. For the current Flash-Sim matrix, use
the fixed compact-normalized traces under `traces/run_test/`; the downloaded
SNIA archive is only needed if you want to regenerate or extend those traces.

## Public Exchange Trace Conversion

This section is optional for regenerating public Exchange inputs. The current
run-test matrix does not require the original tarball because the actual 10k and
30k compact-normalized traces are stored under `traces/run_test/`.

The SNIA Microsoft Exchange archive can be converted directly from the
downloaded tarball. The converter reads the gzip-compressed ETW CSV members in
the archive and emits two aligned inputs:

- MQSim ASCII trace: `time_ns device start_lba_512B size_512B type`.
- Flash-Sim JSON trace: `time`, `start_lha` and `size` in Flash-Sim's 64 B host
  sectors.

Generate the conservative full-page aligned subset used for first-pass
correctness comparison:

```bash
python validation/mqsim_flash/convert_exchange_trace.py \
  ~/下载/Exchange-Server-Traces.tar \
  --output-dir validation/mqsim_flash/public_traces/exchange \
  --name exchange \
  --member-limit 1 \
  --max-requests 50000 \
  --disk-id 0
```

This preserves only requests whose byte offset and size are both 4 KiB aligned,
so it avoids partial-page bitmap effects. The generated files are:

```text
validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_mqsim.trace
validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_flashsim.json
validation/mqsim_flash/public_traces/exchange/exchange_disk0_page_50000_manifest.json
```

Generate a richer sector-exact subset for later partial-page experiments:

```bash
python validation/mqsim_flash/convert_exchange_trace.py \
  ~/下载/Exchange-Server-Traces.tar \
  --output-dir validation/mqsim_flash/public_traces/exchange \
  --name exchange \
  --member-limit 1 \
  --max-requests 50000 \
  --disk-id 2 \
  --allow-partial
```

The sector-exact version keeps 512 B-aligned partial-page requests and should
not be used as the first correctness gate unless both simulators' partial-page
semantics are part of the test.

Replay the fixed 10k compact-normalized trace through both simulators:

```bash
$PY validation/mqsim_flash/run_validation.py \
  --profile flashsim-event-small \
  --external-flash-trace validation/mqsim_flash/traces/run_test/exchange_disk0_page_10k_compact_flashsim.json \
  --external-mqsim-trace validation/mqsim_flash/traces/run_test/exchange_disk0_page_10k_compact_mqsim.trace \
  --external-name exchange_disk0_page_10k_compact \
  --external-address-mode raw \
  --external-precondition none \
  --external-mqsim-preconditioning \
  --external-mqsim-initial-occupancy 50 \
  --skip-build \
  --timeout 900
```

External replay mode first checks that the Flash-Sim JSON trace and MQSim ASCII
trace agree in original byte address, request size, request type, and arrival
time. The default `compact` address mode keeps exact source-page identity while
assigning first-seen source pages to a compact LPA range, then writes that same
normalized trace for both simulators. This avoids the artificial plane hotspots
that `modulo` folding can create when a large public trace is replayed on the
small correctness profile. The default `read-pages` Flash-Sim precondition seeds
all pages that are read in the replay window, avoiding invalid-read failures
caused by reads racing ahead of earlier writes that have not completed yet.
For external traces with a warmup prefix, MQSim stdout may report fewer
serviced requests than generated requests even when the XML aggregate and
Flash-Sim main-trace gates are usable; the harness treats that serviced-count
mismatch as a diagnostic note instead of a correctness failure.

For the fixed traces under `traces/run_test/`, use `--external-address-mode raw`
because they are already compact-normalized. The run-test matrix uses Flash-Sim
runtime `precondition_fill_ratio` and MQSim built-in initial occupancy, rather
than an explicit Flash-Sim pre-trace.

## Profiles

The harness separates correctness profiles from performance-calibration
profiles.

`flashsim-event-small`

: The default smoke/correctness profile for this pre-modern Python simulator.
  It uses Flash-Sim's original compact event-runtime geometry, so it can run
  quickly without allocating the full modern-profile PHY storage. It keeps
  MQSim's ideal mapping table enabled so data-page placement, scheduling, and
  maintenance behavior can be isolated from mapping-cache effects.

`flashsim-event`

: Uses the same event-path timing constants with modern block geometry. This is
  useful for explicit large-geometry checks, but it can be much heavier in the
  current Python implementation because PHY storage is materialized eagerly.

`flashsim-event-finite-cmt`

: Uses the same geometry and timing as `flashsim-event`, but disables MQSim's
  ideal mapping table and sets `CMT_Capacity` to 384 B. For the compact
  validation geometry this is approximately 64 MQSim CMT entries, matching
  Flash-Sim's current finite CMT entry count. Use this profile to diagnose
  mapping-read, CMT hit/miss, and request-latency differences. Translation-page
  granularity still differs between the simulators, so treat this as the next
  validation layer rather than a replacement for the ideal-mapping baseline.
  Reports subtract MQSim mapping reads from raw read commands for the user/data
  read gate, while keeping raw read and mapping-read counts as diagnostics.

`fast18-paper`

: Keeps the SSD geometry and latency values reported in the FAST'18 MQSim
  paper. This is useful for reproducing MQSim behavior, but it is not yet a
  strict Flash-Sim comparison profile because Flash-Sim's event path currently
  has hard-wired compact geometry/timing constants.

`modern-profile` (future)

: A later calibration profile should use a specific target SSD or NAND
  datasheet: PCIe generation/lane count, ONFI/NV-DDR rate and width, page size,
  pages per block, dies/planes, SLC/TLC/QLC latency classes, write-cache size,
  CMT size, and firmware policy knobs.

For correctness validation, do not update MQSim to a modern SSD first. Keep a
pinned reference profile, make Flash-Sim agree on semantics, and only then add
modern-performance calibration as a separate profile.

## Usage

Run the default minimal validation:

```bash
$PY validation/mqsim_flash/run_validation.py
```

Run a named profile:

```bash
$PY validation/mqsim_flash/run_validation.py --profile flashsim-event-small
$PY validation/mqsim_flash/run_validation.py --profile flashsim-event
$PY validation/mqsim_flash/run_validation.py --profile flashsim-event-finite-cmt
$PY validation/mqsim_flash/run_validation.py --profile fast18-paper
```

Run the current `run_test.md` matrix:

```bash
$PY validation/mqsim_flash/run_test_matrix_latest.py
```

This matrix uses the fixed `traces/run_test` inputs, runs 10k with 25/50/75%
precondition and 30k with 25/50% precondition, checks both `cache_bypass=true`
and 64 KiB write-cache mode, and writes:

```text
test_result.md
validation/mqsim_flash/out/run_test_matrix_latest/<run_id>/summary.json
```

If `../MQSim/MQSim` is missing or a specific MQSim case fails, the matrix still
reports the Flash-Sim result and records the MQSim failure/skipped row. To
refresh `test_result.md` from an existing run without rerunning the simulators:

```bash
$PY validation/mqsim_flash/run_test_matrix_latest.py --rerender 20260705_160118
```

Run the aligned read correctness experiment:

```bash
$PY validation/mqsim_flash/run_validation.py --case flush_then_read --skip-build
```

Run a longer mixed trace with direct latency comparison:

```bash
$PY validation/mqsim_flash/run_validation.py --case rich_aligned --skip-build
```

Run the full-page CWDP boundary check:

```bash
$PY validation/mqsim_flash/run_validation.py --case parallel_cwdp --skip-build
```

Run the full-page overwrite/mapping check:

```bash
$PY validation/mqsim_flash/run_validation.py --case overwrite_mapping --skip-build
```

Run the deterministic GC relocation check:

```bash
$PY validation/mqsim_flash/run_validation.py --case gc_pressure --skip-build
```

Run multiple deterministic GC rounds:

```bash
$PY validation/mqsim_flash/run_validation.py --case gc_pressure --gc-rounds 4 --skip-build
```

Run a latency-aligned finite-CMT GC check with spaced verification reads:

```bash
$PY validation/mqsim_flash/run_validation.py --profile flashsim-event-finite-cmt --case gc_pressure --gc-rounds 4 --gap-ns 300000 --skip-build
```

Run the static wear-leveling stress check:

```bash
$PY validation/mqsim_flash/run_validation.py --case wear_leveling --skip-build
```

Use an existing MQSim binary without rebuilding:

```bash
$PY validation/mqsim_flash/run_validation.py --skip-build
```

Artifacts are written under:

```text
validation/mqsim_flash/out/<profile>/<case>/
```

Each run writes both machine-readable and visual reports:

- `summary.json`: full structured data for CI and later analysis.
- `report.html`: a static dashboard for quick visual inspection of the
  correctness gates.

The default case is `write_stream`: a short sequence of full-page writes. The
comparison is intentionally conservative: it checks that both simulators accept
and complete the same request count and that Flash-Sim reports no host-visible
request errors. Latency equality is not asserted yet, because Flash-Sim can
complete writes at the controller-cache boundary while NAND persistence is
reported separately.

The aligned full-page cases (`flush_then_read`, `rich_aligned`,
`parallel_cwdp`, and `overwrite_mapping`) run Flash-Sim with write-cache bypass
and `CWDP` plane allocation by default, while MQSim uses `TURNED_OFF` data-cache
mode. This keeps the comparison focused on FTL mapping, scheduling, and NAND
timing instead of write-cache boundary differences.

For a more intuitive correctness check, use `flush_then_read`. This case runs
one shared trace on both simulators:

- 65 full-page setup writes. Flash-Sim's current event path uses a 64-line write
  cache in normal operation, but this aligned case now bypasses the cache so
  every setup write should become a NAND program.
- A large time gap, so the setup writes can finish before reads.
- 8 full-page measured reads to the earliest written pages.

The important pass/fail gates become visually direct: total request count,
read/write request counts, media read command count, user program command count,
and erase count should match on both simulators. With the default settings this
means 73 total requests, 65 writes, 8 reads, 65 NAND programs, 8 NAND reads, and
0 erases.

`rich_aligned` is the longer latency-oriented case. It keeps `flush_then_read`
as the small explainable baseline, then adds a deterministic mixed measured
window after setup:

- 96 full-page setup writes to create valid data before the measured window.
- A large time gap before the measured window.
- At least 256 measured requests by default: a read-heavy mix of NAND reads to
  already-flushed pages and cold full-page writes.

The report compares latency at two levels:

- Media service latency: command/data transfer plus array execution, excluding
  queue wait. This is the clearest check that the SSD parameters are aligned.
- Transaction turnaround / host-device latency: includes scheduling and queueing
  effects. Large differences here indicate model-policy differences rather than
  a basic NAND timing mismatch.

The media-operation gates use effective page operations. For MQSim, multiplane
program/read commands are converted to the equivalent number of programmed/read
pages before comparison, so a two-plane program command counts as two page
programs.

`parallel_cwdp` is a full-page resource-boundary case. It writes and then reads
pages selected from known MQSim CWDP coordinates: channel fanout, chip fanout,
die boundary, and plane boundary. Exact NAND command counts are diagnostic here
because MQSim may merge requests into multiplane commands; the hard gates are
request counts, Flash-Sim successful completions, and latency diagnostics.

`overwrite_mapping` is a full-page overwrite/mapping case. It deliberately stays
inside the first CWDP die/plane and the first three data chips, so it isolates
overwrite and mapping update behavior from the static-chip boundary. The hard
gates include request counts, successful completions, media read page count,
media program page count, and erase count.

`gc_pressure` is the first strict maintenance-path check. Each round writes 49
blocks in one CWDP plane, overwrites a small hot set, and raises both
simulators' GC thresholds so GC is reached before MQSim's ideal-mapping profile
leaves later requests waiting. By default it runs one relocation GC round: the
expected signature is 1 GC, 0 static WL, 3 relocated pages, and 1 erase. Use
`--gc-rounds N` to run multiple deterministic GC rounds. Multi-round mode spreads
rounds across independent CWDP planes, so the expected signature is `N` GCs, 0
static WL, `3*N` relocated pages, and `N` erases on both simulators. This keeps
each GC independent while still validating repeated maintenance behavior. The
current strict regression has been checked at `N=4`; larger `N` values are useful
stress tests and may expose additional victim-valid-page accounting differences.

`wear_leveling` uses the same full-page single-plane setup but fully invalidates
one hot block and lowers the static WL threshold to 1. Flash-Sim currently
reports the intended behavior: 1 GC, 1 static WL, 8 relocated pages, and 2
erases. The verify window reads back the overwritten hot block, a cold control
block, and the cold block selected by Flash-Sim's current safe-candidate policy,
so relocated WL data must remain readable. MQSim maintenance metrics are
diagnostic for this case rather than pass/fail gates: MQSim's ideal-mapping
configuration does not start static WL in this trace, because its static-WL
implementation selects one coldest block and returns if that block is an unsafe
write frontier such as `Translation_wf`, rather than looking for the next safe
cold block.

The visual report separates correctness gates from diagnostic metrics. Request
counts, read/write counts, Flash-Sim errors, ONFI helper formula consistency,
and simple NAND command accounting are displayed as pass/fail checks. Latency is
shown only as a diagnostic snapshot because the two simulators currently expose
different write-completion boundaries. MQSim reports its XML latency fields in
microseconds; the HTML report converts them to nanoseconds before plotting them
beside Flash-Sim's nanosecond latency fields.

For write-heavy cases, interpret latency carefully:

- Flash-Sim `host avg` is the host-visible completion latency. With the current
  write cache path, this can be a cache-acceptance latency rather than NAND
  persistence latency.
- Flash-Sim `persistence avg` includes waiting in the controller cache until
  the end-of-trace flush plus NAND program time.
- MQSim `device response avg` is the flow-level device response time reported
  by MQSim. In write-cache mode, it is not the same measurement boundary as
  Flash-Sim persistence latency.

## Complete Command: 30k Trace + Precondition

The following command runs the local fixed 30k trace on `small64`, with 25% and
50% precondition, both cache-bypass and 64 KiB cache modes, and writes a
self-contained result markdown plus per-case artifacts:

```bash
cd /home/kkkkaa/文档/PKU/轮转/李萌/Flash-Sim-pre-modern
PY=/home/kkkkaa/文档/PKU/轮转/李萌/.venv/bin/python
$PY validation/mqsim_flash/run_small64_30k_latency_result.py \
  --trace-label 30k \
  --precondition 25 \
  --precondition 50 \
  --cache-mode bypass \
  --cache-mode cache64 \
  --result-path result_small64_30k_precondition.md \
  --out-root validation/mqsim_flash/out/small64_30k_precondition
```
