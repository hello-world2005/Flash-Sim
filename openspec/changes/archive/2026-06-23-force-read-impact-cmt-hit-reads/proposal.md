## Why

The read-impact experiment currently can generate baseline and contended `read` requests that follow different AMU paths because one side may miss CMT and issue `mapping_read` work while the other side hits CMT. That makes the comparison mix compute-channel contention with mapping-cache effects, so the experiment must force both read streams onto the same CMT-hit path.

## What Changes

- Update read-impact trace generation so all generated baseline and contended `read` commands target LPAs whose mappings are already warmed in CMT for both simulator runs.
- Add validation before simulation that the selected read commands are eligible for CMT-hit execution in the preconditioned state.
- Add post-simulation validation that every compared read in both baseline and contended reports has only CMT-hit mapping resolutions and no `mapping_read` resolutions.
- Expose per-request mapping-resolution counts in the request latency JSON report if they are not already available to experiment tooling.
- Fail the read-impact experiment with a clear error if the generated or user-supplied read set would exercise the mapping-read path.
- Preserve the existing paired-trace contract: both traces still contain identical read portions, and only the contended trace prepends compute requests.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `request-resource-contention-experiments`: Read-impact trace generation and result validation must guarantee compared reads are CMT hits in both baseline and contended runs.
- `request-latency-reporting`: JSON request records must expose mapping-resolution counters so experiments can distinguish CMT hits from mapping reads.

## Non-goals

- This change does not alter AMU mapping policy, CMT replacement behavior, GMT/GTD formats, preconditioning semantics, or runtime read translation outside the experiment workflow.
- This change does not change existing request latency JSON/CSV field names; any report addition must be backward-compatible.
- This change does not alter ONFI channel transfer scheduling or command preemption behavior.
- This change does not add a general workload generator for arbitrary CMT warm-up scenarios.

## Impact

- In scope modules and functions:
  - `test_script/request_resource_contention_experiments.py`: read-command selection, paired trace generation, read-impact simulation validation, and error messages.
  - `test_script/test_request_resource_contention_experiments.py`: regression tests for CMT-hit-only read selection and mapping-read rejection.
  - `flash_sim/request_latency_report.py`: additive JSON export of per-request mapping-resolution counts.
  - `test_script/test_request_latency_report.py`: regression coverage for the new JSON field.
- Out of scope modules and functions:
  - `flash_sim/FTL.py`, `flash_sim/PHY.py`, `flash_sim/request_latency_report.py`, `flash_sim/config.py`, and core simulator scheduling behavior unless a small adapter is required to read existing report fields.
- Test targets:
  - Generated read-impact traces choose only preconditioned LPAs that are already resident in CMT.
  - A baseline or contended report containing any compared read with `mapping_read > 0` causes an explicit experiment failure.
  - Matched read comparison still produces one row per read when both reports show all compared reads as CMT hits.
