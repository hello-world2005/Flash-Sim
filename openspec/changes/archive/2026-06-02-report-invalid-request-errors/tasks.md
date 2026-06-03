## 1. Completion Contract

- [x] 1.1 Modify `flash_sim/common.py::Request` to store terminal request metadata needed by this change, including `SUCCESS` / `ERROR` status, optional `error_message`, and any guard needed to prevent duplicate completions.
- [x] 1.2 Modify `flash_sim/HIL.py::_complete_request` or replace it with explicit success/error finalization helpers, and update `flash_sim/Host.py::Host.execute` to consume `REQ_COMP.status` and `REQ_COMP.error_message` and print them into the log output.

## 2. HIL Admission Validation

- [x] 2.1 Add request-domain validation in `flash_sim/HIL.py` before segmentation/data-fetch/FTL dispatch so that invalid `SEARCH` / `COMPUTE` / `STATIC_WRITE` static-range requests and ordinary `WRITE` requests into the static range complete immediately as `ERROR`.
- [x] 2.2 Modify `flash_sim/HIL.py::receive_pcie_message`, `flash_sim/HIL.py::segment`, and related cache-registration/data-fetch paths so rejected requests do not register cache entries, do not send `*_DATA_REQ`, and do not submit work to `FTL` or `PHY`.

## 3. Read Error Propagation

- [x] 3.1 Modify `flash_sim/FTL.py::Address_Mapping_Unit.translate_and_submit` and `flash_sim/FTL.py::Address_Mapping_Unit._handle_mapping_response` to convert missing mapping pages, invalid mapping slots, and `INVALID_PPA` responses into request-scoped failures and clear any dependent `waiting_for_mapping_trans` state.
- [x] 3.2 Modify `flash_sim/PHY.py::_read_from_storage` and the transaction-serviced callback flow, plus `flash_sim/HIL.py::_on_transaction_serviced`, so host-backed read and mapping-read faults are surfaced as failed transactions with error messages instead of uncaught exceptions.

## 4. Verification

- [x] 4.1 Add or update unit tests in `tests/` for `REQ_COMP` payload/status propagation, HIL-side invalid request rejection, and AMU/PHY failure-to-error conversion.
- [x] 4.2 Add or update end-to-end regression traces and simulator tests in `test_case/` and `tests/` covering invalid `SEARCH` / `COMPUTE`, invalid static-region `WRITE`, unmapped `READ`, and `READ` access to free or invalid sectors, and verify the run logs contain the expected `SUCCESS` / `ERROR` completion lines.
