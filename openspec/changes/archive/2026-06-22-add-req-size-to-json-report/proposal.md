## Why

Request latency JSON reports currently identify each request by type, timing, and status, but they do not expose the original trace `size`. Downstream analysis needs that size beside each request record so latency can be correlated with request length without re-reading the input trace.

## What Changes

- Add the original request `size` to every per-request record in the JSON latency report.
- Preserve the existing report shape, timing fields, CSV output, and simulator behavior.
- Add focused test coverage that verifies the JSON report carries `size` values from the input requests.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `request-latency-reporting`: JSON request records include the original request size.

## Impact

- In scope: `flash_sim/request_latency_report.py`, request registration/export behavior, and request-latency reporting tests.
- Out of scope: CSV column changes, timeline visualizer output, trace parser semantics, request scheduling, and latency calculations.
- Test target: a request-latency recorder/export test that registers requests with different `size` values and asserts each JSON request entry includes the matching `size`.

## Non-goals

- Do not alter the meaning or units of `Request.size`.
- Do not change CSV output unless a future change explicitly asks for it.
- Do not backfill size from generated report files or infer it from address ranges during export.
