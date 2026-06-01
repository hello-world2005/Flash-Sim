## Why

The current `test_read_write.json` trace exposes a read-after-write gap: after a host `WRITE` is acknowledged from controller cache, a later `READ` can still fall through to AMU mapping lookup and fail on an unmapped logical page. This means the simulator is not treating cached write transactions as the authoritative source for newly written logical data before flash persistence, and cache-pressure flushing also does not currently force those buffered writes to drain ahead of new reads.

## What Changes

- Update the `HIL` write path so `WRITE` requests are segmented before caching, and each segmented user-write transaction is stored in the controller `Data_Cache` together with its logical address metadata.
- Update the `HIL` read path so segmented `READ` transactions probe `Data_Cache` by logical address first; cache hits are completed directly and only cache misses continue into `FTL` / AMU translation.
- Change cache-pressure handling so a full `Data_Cache` flushes all buffered user-write entries to AMU as transaction-granularity write work, then schedules those writes ahead of new reads until the buffered data is drained into flash.
- Add regression coverage for the end-to-end read-after-write trace and for cache-full behavior that must preserve write visibility while draining buffered data.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `host-device-request-flow`: `WRITE` buffering and `READ` completion semantics must reflect transaction-granularity cache entries keyed by logical address, so read-after-write hits can complete from controller cache without consulting mapping state.
- `ftl-scheduling-and-media-model`: TSU scheduling behavior must support a cache-pressure mode where flushed user writes are submitted ahead of reads until the buffered write backlog is persisted.

## Non-goals

- Changing how `SEARCH`, `COMPUTE`, or `STATIC_WRITE` requests are segmented or scheduled outside the shared cache-pressure priority rule.
- Reworking AMU mapping formats, GTD/GMT/CMT structures, or NAND timing constants.
- Changing host-visible write completion semantics beyond making the acknowledged cached data readable before it reaches the flash array.

## Impact

- In scope modules/functions: `flash_sim/HIL.py::HIL.segment`, `flash_sim/HIL.py::HIL.receive_pcie_message`, `flash_sim/HIL.py::HIL._tile_data`, `flash_sim/HIL.py::Data_Cache`, `flash_sim/HIL.py::Cache_Manager.query_cache`, `flash_sim/HIL.py::Cache_Manager.cache_write`, `flash_sim/HIL.py::Cache_Manager.write_flush`, and `flash_sim/FTL.py::TSU` scheduling entry points.
- In scope tests: `tests/test_data_cache.py` and an end-to-end regression driven by `test_case/test_read_write.json` or equivalent simulator invocation coverage.
- Out of scope: parser/trace schema changes, host queue bookkeeping, static-area search/compute execution behavior, and PHY media execution timing.
