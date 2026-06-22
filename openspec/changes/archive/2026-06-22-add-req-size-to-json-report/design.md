## Context

The event-driven simulator records each input request in `RequestLatencyRecorder` and exports a JSON report through `RequestLatencyRecorder.export()` / `dump_json()`. The recorder already receives the original `Request` object during registration, so the request `size` can be carried from the trace into each JSON request entry without changing scheduling or timing logic.

## Goals / Non-Goals

**Goals:**

- Ensure every JSON request latency entry includes the original `Request.size`.
- Keep the value synchronized if a request is observed again after registration.
- Add test coverage that loads a generated JSON report and validates the `size` field.

**Non-Goals:**

- Do not add a CSV `size` column.
- Do not change trace parsing, request segmentation, cache behavior, or latency formulas.
- Do not infer size from transaction counts or address ranges.

## Decisions

- Store `size` on `RequestLatencyState` from the original `Request`.
- Export the stored value as a top-level JSON field named `size` beside `type` and `lha_start`.
- Validate the behavior at the end-to-end report level so the file written under `report/` is covered, not only the in-memory export method.

## Design Rationale

The report already treats `RequestLatencyState` as the stable per-request snapshot for identifiers, trace timing, address, status, and breakdown data. Keeping `size` there follows the same pattern as `lha_start` and avoids coupling report export back to the input trace file. A top-level `size` field is easier for downstream scripts to consume than embedding the value in metadata or interval details.

## Risks / Trade-offs

- Existing downstream JSON readers that assume a closed schema may see an extra field. Mitigation: this is an additive field on each request record and does not rename or remove existing keys.
- Requests created internally without a trace size may report `0` or `None` depending on their `Request` state. Mitigation: export the exact `Request.size` value rather than guessing.

## Migration Plan

No data migration is required. New reports include the field; existing reports remain readable as historical output.

## Open Questions

None.
