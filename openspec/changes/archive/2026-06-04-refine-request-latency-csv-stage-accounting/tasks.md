## 1. Report Semantics

- [x] 1.1 Update `flash_sim/request_latency_report.py` so `Mapping处理时间` absorbs the full `MAPPING_READ` dependency path while `TSU中等待时间` measures only effective USER transaction queueing.
- [x] 1.2 Keep `完成时间` host-visible and preserve separate `PCIe返回状态耗时` and `PCIe返回数据耗时` columns so completed rows remain additive when summing every column except the final payload-return column.

## 2. Verification

- [x] 2.1 Update request-latency unit coverage to verify additive completed rows, mapping-path absorption, and USER-only TSU wait semantics.
- [x] 2.2 Update end-to-end request-latency regressions, including focused read/write traces, to verify mapping-dependent reads, cache-hit reads, and host-visible completion accounting.
- [x] 2.3 Verify a real `test_case/test_trace.json` run produces completed CSV rows whose non-payload columns sum to `完成时间 - Issue时间`.

## 3. Change Sync

- [x] 3.1 Sync `proposal.md`, `design.md`, and `specs/request-latency-reporting/spec.md` with the final CSV accounting semantics used by the implementation.
