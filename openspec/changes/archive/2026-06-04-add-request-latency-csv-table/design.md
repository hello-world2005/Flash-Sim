## Context

`flash_sim/request_latency_report.py` currently exports one JSON report per trace. The JSON keeps detailed interval lists and stage breakdowns for both host-visible request completion and, for buffered writes, backend persistence completion. That structure is good for tooling but awkward for direct inspection in spreadsheets.

The requested CSV is not a second independent metrics pipeline; it is a flattened table view over the same per-request state. The main complexities are:

- write requests complete at the host before backend media persistence finishes, so a useful single-row CSV must combine host-facing completion timestamps with persistence-side TSU/PHY timing;
- `amu_mapping_wait == 0` does not uniquely imply a CMT hit, because a request can bypass a mapping read through GMT or direct CMT state;
- the simulator does not currently send explicit PCIe data-return messages for read/search/compute completions, so the CSV return-latency column must account for payload transfer time without breaking the existing JSON semantics.

## Goals / Non-Goals

**Goals:**

- Preserve the current JSON request latency report and emit an additional CSV file beside it.
- Define deterministic CSV columns and row-derivation rules for read, write, search, and compute requests.
- Record enough metadata to distinguish CMT hits from other mapping-resolution paths in the CSV cache-hit column.
- Include PCIe return/status latency in the CSV in a way that counts response payload transfer time for non-write requests.

**Non-Goals:**

- Replacing the JSON report with CSV.
- Refactoring the simulator into a fully symmetric PCIe response-data transport model.
- Reworking unrelated timeline-reporting code or changing PHY/TSU execution behavior.

## Decisions

1. Keep JSON export as the source of truth and add a parallel CSV export path.
   `RequestLatencyRecorder` will continue to build the same per-request state and JSON payload, then derive CSV rows from that state using a dedicated formatter.

   Alternatives considered:
   - Replace JSON with CSV-only output.
     Rejected because the JSON is already consumed by tests and is more expressive for interval-level debugging.
   - Generate CSV by reparsing the dumped JSON file from disk.
     Rejected because it introduces needless I/O and duplicates serialization logic.

2. Add lightweight AMU cache-resolution annotations instead of inferring cache hits from timing.
   A new recorder hook from the AMU path will note whether a request resolved through CMT, GMT, or a mapping-read fallback. The CSV cache-hit column will report true CMT hits only; `SEARCH` and `COMPUTE` will emit `/`.

   Alternatives considered:
   - Infer cache hits from `amu_mapping_wait == 0`.
     Rejected because GMT hits would be misclassified as cache hits.
   - Infer cache hits from transaction type patterns after the fact.
     Rejected because those patterns still do not distinguish CMT from GMT.

3. Build the CSV timing columns from a request-type-specific view over existing host and persistence breakdowns.
   For reads/searches/computes, CSV stage columns come from the host-facing breakdown. For writes/static writes, the CSV keeps host issue/completion and PCIe handshake timing from the host-facing breakdown, but uses persistence-side TSU/PHY breakdowns for mapping/backend execution columns.

   Alternatives considered:
   - Use host-facing breakdowns only.
     Rejected because buffered writes would show no TSU or PHY work, making the CSV misleading.
   - Use persistence timings only for writes, including completion time.
     Rejected because the row would stop matching the actual request completion observed by the host.

4. Compute the CSV “PCIe return/status latency” column as the host-facing return breakdown plus a synthetic payload-return term for non-write requests.
   The existing JSON `pcie_device_to_host` breakdown remains unchanged. The CSV formatter will add an estimated payload-transfer duration based on request size and the same PCIe bandwidth/overhead rules already used by `PCIe_link`.

   Alternatives considered:
   - Change simulator execution to emit explicit read/search/compute data-return PCIe messages.
     Rejected for this change because it would alter execution behavior and existing latency breakdown semantics more broadly than needed.

## Design Rationale

The safest implementation is to treat the CSV as a presentation layer over the current recorder state, with only one small instrumentation addition for AMU cache-resolution metadata. That gives us the requested spreadsheet-friendly output without destabilizing the JSON report or rewriting the simulator’s event model.

Separating “JSON truth” from “CSV view” also makes future report changes easier: we can refine column definitions without forcing downstream consumers to migrate off the existing JSON schema.

## Risks / Trade-offs

- [Risk] CSV row semantics for writes could be misunderstood because they mix host completion and persistence phases. → Mitigation: document the derivation rules in the spec and keep completion time explicitly host-facing.
- [Risk] Synthetic PCIe return latency may diverge from a future explicit response-data transport model. → Mitigation: base the estimate on the same bandwidth and payload sizing rules already used by `PCIe_link`, and isolate the calculation in one formatter helper.
- [Risk] Extra metadata fields could accidentally break JSON-based tests if they assume exact key sets. → Mitigation: keep existing keys intact and only add fields in a backward-compatible way, then extend tests accordingly.

## Migration Plan

1. Extend `RequestLatencyRecorder` with CSV path derivation, CSV dumping, and row formatting helpers.
2. Add AMU cache-resolution hooks so the recorder can distinguish CMT hits from GMT/mapping-read paths.
3. Update engine export flow to write both JSON and CSV reports.
4. Add unit and end-to-end tests that validate CSV creation and representative row values.
5. Keep the old JSON path and behavior intact so rollback simply removes the CSV export call sites and cache-hit annotations.

## Open Questions

- None. The remaining work is implementation detail rather than product ambiguity.
