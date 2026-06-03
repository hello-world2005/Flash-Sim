## Context

The current simulator has request objects and a `REQ_COMP` message path, but completion metadata is underspecified and invalid accesses still escape as uncaught `ValueError`s. In the current code:

- `HIL._complete_request(...)` sends `REQ_COMP` with a single `"completed"` status string and no error detail.
- `Host.execute(...)` stores that string on `req.status` and prints the request, but has no structured error field to log.
- `HIL.receive_pcie_message(...)` admits requests directly into segmentation, data fetch, cache lookup, or FTL submission without explicit address-domain validation.
- `Address_Mapping_Unit.translate_and_submit(...)` and `_handle_mapping_response(...)` raise synchronous read-path exceptions for missing mapping pages, invalid mapping slots, and invalid PPAs.
- `PHY._read_from_storage(...)` raises asynchronous exceptions for invalid mapping pages, invalid user pages, and invalid sectors.

That means invalid requests either fail too late or crash the whole run, while the host never receives a terminal `ERROR` completion carrying the diagnosis.

## Goals / Non-Goals

**Goals:**

- Make request completion explicit and host-visible: every `REQ_COMP` carries `SUCCESS` or `ERROR` plus optional error text.
- Reject illegal request-type versus address-domain combinations deterministically, before unnecessary data fetch or FTL scheduling.
- Convert read translation and media-access faults into request-scoped failures that still travel through the normal completion path.
- Ensure the same status and error information is printed into the simulator log stream.
- Preserve current success-path timing semantics: valid writes still complete when controller cache accepts data, and valid reads/searches/computes still complete on their existing service milestones.

**Non-Goals:**

- Redesigning submission/completion queue mechanics or PCIe latency modeling.
- Introducing host retry logic, request cancellation, or partial-success semantics.
- Changing valid `STATIC_WRITE` behavior, NAND timing, or GC policy.
- Refactoring unrelated internal assertions that are not attributable to a host request.

## Decisions

### Decision: HIL remains the single owner of terminal request completion

`HIL` will become the canonical place that transitions a request into a terminal state and emits `REQ_COMP`. The request object will carry terminal metadata:

- `status`: `SUCCESS` or `ERROR`
- `error_message`: optional string, `None` on success
- an idempotence guard so the same request cannot emit multiple completions when late callbacks arrive

`REQ_COMP` payloads will always include both `status` and `error_message`, and `Host` will mirror them back onto the `Request` instance before logging.

Alternatives considered:

- Catch all exceptions at `Engine.Run()` and synthesize completions there. Rejected because the engine does not own request lifecycle state and cannot safely unwind partially submitted transactions.
- Let each lower layer send completions directly. Rejected because it would fragment completion policy and make duplicate completions more likely.

### Decision: Perform admission-time domain validation in HIL before fetch or dispatch

Illegal request-type versus address-domain combinations will be rejected in `HIL` before host data fetch or FTL submission:

- `SEARCH`, `COMPUTE`, and `STATIC_WRITE` must stay fully inside the static region.
- Ordinary `WRITE` must stay fully inside the random-access region.

This keeps invalid requests from polluting the cache, generating meaningless static addresses, or triggering host payload fetches that will be discarded.

Alternatives considered:

- Validate only inside `FTL.get_static_address(...)` or `AMU.get_plane_address_for_lpa(...)`. Rejected because some invalid requests would already have fetched payload or mutated cache state before failing.
- Validate only by catching downstream `ValueError`s. Rejected because error messages would depend on incidental call order and would be harder to make deterministic.

### Decision: Split failure handling into synchronous request errors and asynchronous transaction failures

Two failure channels are needed:

- Synchronous request errors for admission/translation checks that happen while handling the incoming PCIe message.
- Asynchronous transaction failures for media-read problems discovered later in `PHY`.

The design will introduce a request-scoped failure representation used across layers. The concrete implementation can be either:

- a small custom exception class for synchronous errors, plus
- transaction failure metadata (`failed`, `error_message`) for asynchronous callbacks

The important contract is:

- `AMU` and `PHY` stop raising raw `ValueError` for request-attributable read faults
- request-attributable failures are converted into request or transaction error objects carrying a user-facing message
- those errors propagate back to `HIL`, which sends one terminal `REQ_COMP`

Alternatives considered:

- Parse existing `ValueError` strings in `HIL`. Rejected because it is brittle and ties behavior to message text.
- Treat all read faults as internal simulator bugs. Rejected because the user explicitly wants them surfaced as request errors, not fatal crashes.

### Decision: Asynchronous read faults propagate through transaction-serviced callbacks

`PHY` already broadcasts transaction-serviced callbacks to `HIL`, `AMU`, `Block_Manager`, and `TSU`. We will reuse that path for failures:

- when a host-backed `USER_READ` or `MAPPING_READ` encounters an invalid page or sector, `PHY` marks the transaction failed and broadcasts it instead of throwing out of the event loop
- `AMU` detects failed `MAPPING_READ` transactions, clears any waiting read dependencies for the affected logical addresses, and refrains from submitting dependent `USER_READ`s
- `HIL` detects failed source transactions and immediately finalizes the owning request with `ERROR`

This preserves the event-driven architecture and avoids adding a second out-of-band failure bus.

Alternatives considered:

- Introduce a dedicated failure callback separate from transaction-serviced. Rejected because it duplicates existing fan-out wiring and complicates ordering.
- Let failed `MAPPING_READ`s leave dependent reads pending forever. Rejected because it deadlocks the request instead of completing it.

### Decision: Completion logging is emitted at the same point as request finalization

The simulator currently captures stdout/stderr into log files. To guarantee visibility even when `debug_info(...)` is disabled, terminal request completion will print a single structured line containing request identity, final status, and any error message.

That print happens in the same helper that emits `REQ_COMP`, so success and error logs stay consistent with what the host receives.

Alternatives considered:

- Rely on `debug_info(...)`. Rejected because it is currently a no-op.
- Log only at the host side. Rejected because lower-layer failures should still be visible even if host-side formatting changes later.

## Design Rationale

The key design choice is to keep completion authority centralized in `HIL` while making lower layers failure-aware instead of exception-heavy. That matches the simulator's existing architecture:

- `Host` and PCIe model request delivery and completion visibility.
- `HIL` already owns request segmentation, cache interaction, and the current completion send.
- `AMU` and `PHY` own the deeper validation knowledge needed to tell whether a read can be translated or serviced.

By letting `AMU` and `PHY` produce structured failures and letting `HIL` terminate the request exactly once, we get deterministic error behavior without flattening the existing layering.

## Risks / Trade-offs

- [Duplicate completion after an error] -> Add an idempotent terminal-state guard on `Request` and make late transaction callbacks no-op for already completed requests.
- [Waiting mapping-read dependents leak after a failed mapping read] -> `AMU` must clear `waiting_for_mapping_trans[lpa]` entries for failed lookups when it propagates the error.
- [Over-validating request domains changes behavior beyond the user's ask] -> Scope admission-time domain checks to the explicitly requested invalid cases: static-path ops outside static range and ordinary writes into static range.
- [Error propagation affects GC or internal maintenance transactions] -> Restrict request-level failure handling to transactions with a non-`None` `source_req`; internal GC/assertion failures remain internal errors.
- [Log spam for multi-transaction read failures] -> Emit logs only at request finalization, not once per failed transaction.

## Migration Plan

1. Extend request/completion data structures so status and error text are always representable.
2. Add HIL request-finalization helpers and admission-time validation before touching host data fetch or FTL submission.
3. Convert AMU translation failures and PHY media-read failures into request/transaction failure propagation.
4. Update Host logging and any tests that assert old `"completed"` completion payloads.
5. Add regression traces and unit tests for invalid requests and invalid read targets.

Rollback is straightforward: revert the change set and the simulator returns to exception-driven failure behavior. No persisted data or external protocol migration is involved beyond the in-repo `REQ_COMP` payload contract.

## Open Questions

- None for proposal scope. If implementation uncovers existing tests or tooling that rely on the literal string `"completed"` in `REQ_COMP`, we should update those consumers in the same change rather than preserving dual status formats.
