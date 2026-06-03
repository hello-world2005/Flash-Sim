## Why

The simulator currently lets several invalid request paths escape as uncaught `ValueError`s inside `HIL`, `AMU`, or `PHY`, which terminates the run instead of completing the offending request in a host-visible way. As the request mix now includes random-access IO plus static-area search/compute traffic, we need explicit request-level error semantics so invalid accesses can be diagnosed, logged, and returned through `REQ_COMP` without crashing the simulation.

## What Changes

- Add request validation for illegal address-domain combinations, including `SEARCH` / `COMPUTE` targeting random-access space and ordinary `WRITE` targeting the static area while still allowing `STATIC_WRITE`.
- Convert read-path translation and media-access failures into request-level completion errors when a `READ` targets an LPA with no mapped PPA or reaches free / invalid sectors.
- Standardize request completion so every `REQ_COMP` carries `SUCCESS` or `ERROR` plus an error message when applicable, and ensure the same information is printed in logs.
- Add regression coverage for invalid-domain requests, unmapped-read failures, invalid-sector read failures, and successful propagation of error metadata through host-visible completion.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `host-device-request-flow`: request completion semantics now distinguish `SUCCESS` and `ERROR`, and invalid host requests must complete through `REQ_COMP` with an attached error message instead of aborting the run.
- `ftl-scheduling-and-media-model`: AMU and PHY read-path failures must be surfaced as request-level errors, and static-chip versus random-access address-domain violations must be rejected deterministically.

## Non-goals

- Changing the trace schema, queue-depth model, or PCIe timing model.
- Adding host-side retry, recovery, or automatic request rewriting after an `ERROR` completion.
- Redefining valid static-area `STATIC_WRITE` behavior or changing search / compute media timing.
- Changing successful request payload semantics beyond attaching completion status and optional error text.

## Impact

- In scope modules/functions: `flash_sim/common.py::Request`, `flash_sim/HIL.py::HIL.segment`, `flash_sim/HIL.py::HIL.receive_pcie_message`, `flash_sim/HIL.py::_complete_request` and related completion helpers, `flash_sim/Host.py::Host.execute`, `flash_sim/FTL.py::Address_Mapping_Unit.translate_and_submit`, `flash_sim/FTL.py::_handle_mapping_response`, and `flash_sim/PHY.py::_read_from_storage` plus the transaction-serviced callback path.
- In scope tests: request-flow unit tests around `REQ_COMP` payloads, end-to-end traces for illegal `SEARCH` / `COMPUTE` and illegal static-region `WRITE`, and read regressions that exercise unmapped-LPA and invalid-sector failures without terminating the full simulation.
- Out of scope: CLI-only simulator command error handling, non-request internal assertions unrelated to a host request, and unrelated GC / scheduling policy changes.
