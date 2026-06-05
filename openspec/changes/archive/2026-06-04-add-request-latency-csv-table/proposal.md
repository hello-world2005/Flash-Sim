## Why

The current request latency report is machine-readable JSON, but it is cumbersome to inspect quickly when comparing per-request timing components across a long trace. We need a stable CSV table that preserves the existing JSON output while presenting each request as one row with the timing columns most useful for manual analysis.

## What Changes

- Extend request latency reporting to emit a CSV file alongside the existing JSON file under `report/`.
- Add per-request CSV columns for issue time, request type, completion time, SQ wait, PCIe request-send time, cache-hit status, mapping-processing time, TSU wait, PHY transfer time, PHY array-operation time, and PCIe return/status time.
- Distinguish CSV row derivation rules for buffered writes versus read/search/compute requests so write rows still include backend persistence timing without changing the existing JSON schema semantics.
- Record enough request-latency metadata to distinguish true CMT cache hits from GMT or mapping-read fallbacks when populating the CSV cache-hit column.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `request-latency-reporting`: request latency export now includes a CSV table view with defined columns and row-derivation rules in addition to the existing JSON report.

## Non-goals

- No replacement or removal of the current JSON report format.
- No redesign of the simulator's PCIe execution model beyond what is needed to compute the CSV summary columns consistently with current behavior.
- No changes to unrelated timeline visualization output or trace-generation behavior.

## Impact

- In scope modules/functions: `flash_sim/request_latency_report.py`, any lightweight instrumentation hooks needed in `flash_sim/FTL.py`, and the report-export call sites in `flash_sim/engine.py` / `flash_sim/main.py` if an additional CSV path needs to be surfaced.
- Out of scope modules/functions: trace parser semantics, PHY timing behavior, and unrelated report consumers outside the request-latency export path.
- Primary test target: verify that a generated CSV file exists beside the JSON report and that representative read/write/search/compute rows expose the expected flattened timing columns, especially cache-hit and PCIe return-time handling.
