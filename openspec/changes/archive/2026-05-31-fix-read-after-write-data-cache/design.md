## Context

The current event-driven request path already segments `READ` and `WRITE` requests in `HIL`, and the in-repo unit tests show that `Cache_Manager.query_cache()` can satisfy a `USER_READ` from cached user-write payload after the write transaction has been buffered. However, the end-to-end `test_case/test_read_write.json` trace still fails with `Read request accessing non-existing mapping page`, which shows that the integrated request flow is letting a read fall into AMU translation before the controller cache has registered a matching write-side logical entry.

The timing of the failing trace is important. `WRITE_REQ` is segmented immediately, but the current code only writes user data into `Data_Cache` after `WRITE_DATA` returns from the host. A subsequent `READ_REQ` can therefore reach `HIL` while the write is still waiting on payload fetch, find no cache entry, and incorrectly consult flash mapping state for data that should already be considered controller-resident for ordering purposes.

The current cache model also splits user-write state across raw cache lines and `pending_user_pages`, while `write_flush()` submits buffered writes to AMU without changing TSU scheduling priority. That makes it hard to express the requested behavior that cache pressure should drain accumulated writes into flash ahead of new reads.

## Goals / Non-Goals

**Goals:**
- Register segmented `WRITE` transactions in `Data_Cache` as soon as `HIL` receives the request, with each cached entry carrying its logical address.
- Let `READ` requests consult those cached logical entries before AMU mapping lookup, and complete directly with `REQ_COMP` when all requested transactions are satisfied in cache.
- Preserve the existing write completion semantic that host writes complete once the controller has accepted the request into cache.
- Add a cache-pressure drain mode that flushes all buffered cache entries to AMU and temporarily prioritizes flushed user writes over reads until the backlog is persisted into the flash array.

**Non-Goals:**
- Changing parser or trace schema formats.
- Redesigning mapping-page layout, AMU translation policy, or PHY timing.
- Reordering `SEARCH` / `COMPUTE` traffic on static chips except where they share the same cache flush entry point.
- Introducing fully modeled host-visible read payload return buffers beyond the existing completion-oriented simulator interface.

## Decisions

### Decision: Split write buffering into entry registration and payload hydration

`HIL` will continue to segment `WRITE_REQ` immediately, but after segmentation it will register each user-write transaction in `Data_Cache` before host payload fetch completes. The cached entry becomes the controller-side record for that logical transaction and carries at least `lpa`, `bitmap`, request identity, and a `data_ready` or equivalent payload-readiness marker. When `WRITE_DATA` arrives, `HIL._tile_data()` fills the already-registered cached entries instead of treating cache insertion as a post-data step.

Alternative considered: keep the current model where a write does not enter cache until payload fetch completes. Rejected because the failing trace shows that a later `READ_REQ` can legally reach `HIL` before `WRITE_DATA`, which means delayed cache registration cannot prevent the invalid mapping lookup.

### Decision: Make logical transaction entries the canonical read-hit source

For buffered user writes, `Data_Cache` will be treated as a transaction directory keyed by logical address, with bitmap/payload contents attached to each entry. `READ_REQ` handling will segment into `USER_READ` transactions, probe the directory by logical address, and treat each fully covered cached transaction as a hit. If every segmented read transaction hits, `HIL` sends `REQ_COMP` directly over PCIe without entering `FTL`; if only part of the request hits, the hit transactions are completed immediately while only the misses continue into AMU translation, and the source request remains open until those misses finish.

Alternative considered: keep raw sector-line lookup as the main cache structure and reconstruct transaction coverage on demand. Rejected because the current split between line storage and pending-page storage duplicates state, obscures whether a logical write has been admitted to cache at all, and does not naturally model the user-request-level hit rule the change requires.

### Decision: Preserve direct read completion semantics on cache hits, even before flash persistence

The new behavior intentionally treats a matching cached logical write entry as sufficient to short-circuit flash-side mapping lookup for reads. When cached payload is already available, `HIL` can copy it into the read transaction; when the simulator only needs completion semantics, the presence of the matching cached logical entry is still enough to send `REQ_COMP` directly.

Alternative considered: require AMU validation or flash persistence before a read can complete. Rejected because that would preserve the current read-after-write failure mode and contradict the requested behavior that logical-address cache matches should complete directly through PCIe.

### Decision: Introduce an explicit cache-pressure drain mode shared by Cache Manager and TSU

When a new write would overflow `Data_Cache`, `Cache_Manager.write_flush()` will package every buffered cache entry into generated write requests and submit them to AMU. The flush path will also raise an internal drain-mode flag plus an outstanding-flushed-write count (or equivalent bookkeeping). While that drain mode is active, TSU will prefer `USER_WRITE` over `USER_READ` on normal chips so the flushed backlog reaches the flash array before new reads overtake it. The drain flag is cleared only after all flush-generated user writes complete.

Static-write entries may be flushed through the same AMU entry point, but their execution order remains governed by the static-chip path; the required priority inversion only applies to normal user writes competing with reads on non-static chips.

Alternative considered: flush once and immediately return to the existing read-before-write TSU order. Rejected because reads could continue to overtake the flush-generated writes, leaving the controller in the same inconsistent visibility window the user wants to close under cache pressure.

## Design Rationale

This design aligns the cache model with the simulator's completion semantics instead of with flash persistence timing. A host `WRITE` is already considered complete once the controller accepts it, so the controller must also become the authoritative source for later overlapping reads from that point onward. Registering logical cache entries at segmentation time is the smallest structural change that makes that guarantee true even when PCIe message ordering allows reads to arrive before `WRITE_DATA`.

Using one logical transaction directory also simplifies the mental model. Instead of asking whether the cache contains enough individual sectors and whether those sectors belong to an admitted write, the read path can answer a single question: "does the controller already own this logical transaction?" That same directory becomes the natural source for full-cache flush generation.

The drain-mode priority inversion is intentionally scoped. It only applies while a cache-full flush backlog exists, so the steady-state TSU policy remains unchanged, but the simulator gets a deterministic rule for draining controller-resident writes into flash before new reads can keep bypassing them.

## Risks / Trade-offs

- Matching reads on logical-address cache entries can make buffered writes visible before their payload returns from the host. -> Mitigation: keep explicit payload-readiness metadata on cached entries and populate read payloads from cache when available; the current simulator interface is completion-oriented, so direct completion remains acceptable even when no host-visible read buffer is modeled.
- A temporary write-first drain mode can increase read latency under sustained write pressure. -> Mitigation: activate the mode only when cache capacity is exhausted, and clear it immediately when the flush-generated write backlog reaches zero.
- Tracking outstanding flush-generated writes across HIL, AMU, TSU, and PHY callbacks can be error-prone. -> Mitigation: tag generated flush requests or transactions explicitly so completion accounting does not depend on inferring origin from queue state.
- Replacing the current line-map plus pending-page split with a transaction-centric directory may invalidate some low-level assumptions in unit tests. -> Mitigation: update tests to assert logical-entry behavior first, then preserve payload/bitmap expectations as secondary checks.

## Migration Plan

1. Update the OpenSpec delta requirements for host-side request flow and TSU scheduling so the desired behavior is explicit before code changes land.
2. Refactor `Data_Cache` / `Cache_Manager` to register segmented write entries at `WRITE_REQ` time and hydrate them on `WRITE_DATA`.
3. Update `HIL.receive_pcie_message()` so read hits complete directly from cache and only misses proceed to `FTL`.
4. Add flush bookkeeping and TSU drain-mode priority handling, then wire completion callbacks to clear the drain state.
5. Add or update regression coverage for the failing `test_read_write.json` path plus cache-full flush behavior.

Rollback is straightforward because the change is internal to controller-side cache and scheduler behavior: revert the new cache-entry registration path and disable drain-mode priority inversion to restore the current post-`WRITE_DATA` caching model.

## Open Questions

- None for proposal scope; the implementation should keep static-write flush submission compatible with the dedicated static-chip scheduling path while applying write-before-read drain priority only to normal user-write traffic.
