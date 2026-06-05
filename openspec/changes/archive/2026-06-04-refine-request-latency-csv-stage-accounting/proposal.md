## Why

The current CSV report needs a stricter accounting contract than the existing change artifacts describe. The implementation should make completed rows additive against the host-visible completion timestamp, while also making the mapping-related dependency path easier to interpret.

More specifically:

- `Mapping处理时间` should absorb the full `MAPPING_READ` dependency path, including mapping wait, mapping-side TSU queueing, and mapping-side PHY work.
- `TSU中等待时间` should only describe the effective queueing of the host-visible user transaction after request ingress and mapping resolution are both ready.
- `完成时间` should remain the host-visible timestamp when the request receives `REQ_COMP`, while response payload transfer remains a separate trailing column.

## What Changes

- Redefine `mapping_time` so it covers the entire mapping-resolution phase from the end of request-side PCIe ingress to the end of the related mapping activity.
- Redefine `tsu_wait_time` so it only measures USER transaction waiting between TSU submission and the first PHY command issue, after excluding earlier request-ingress or mapping-dependent time.
- Keep `pcie_status_return_time` and `pcie_data_return_time` split, and make every completed row additive when summing all columns except the data-return column.
- Update regression coverage and OpenSpec artifacts so the documented semantics match the implemented report.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `request-latency-reporting`: refine CSV stage accounting so mapping absorbs the full mapping dependency path, USER TSU wait is isolated, and host-visible completion rows remain additive.

## Impact

- In scope modules/functions: `flash_sim/request_latency_report.py`, any lightweight recorder hooks needed by request-latency accounting, and request-latency regression tests under `test_script/`.
- Out of scope modules/functions: NAND timing constants, GC policy, preconditioning semantics, trace generation logic, and unrelated report consumers.
- Test targets: focused request-latency unit tests, end-to-end mapping-miss and cache-hit traces, and a real `test_case/test_trace.json` run that verifies additive completed rows.

## Non-goals

- No redesign of AMU, TSU, or PHY scheduling behavior.
- No replacement of the JSON request-latency report with CSV-only output.
- No change to the meaning of the final response-payload column beyond keeping it separate from host-visible completion accounting.
