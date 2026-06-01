## 1. Reshape HIL data-cache entry lifecycle

- [x] 1.1 Modify `flash_sim/HIL.py::Data_Cache` and `flash_sim/HIL.py::Cache_Manager` to store transaction-granularity write entries keyed by logical address, including payload-readiness and flush bookkeeping metadata.
- [x] 1.2 Modify `flash_sim/HIL.py::HIL.segment`, `flash_sim/HIL.py::HIL.receive_pcie_message`, and `flash_sim/HIL.py::HIL._tile_data` so `WRITE_REQ` registers cache entries before host data arrives and `WRITE_DATA` hydrates those existing entries instead of inserting them for the first time.
- [x] 1.3 Modify `flash_sim/HIL.py::Cache_Manager.query_cache` and related helper methods so `READ_REQ` completes directly from logical-address cache hits and forwards only cache misses into `flash_sim/FTL.py::FTL.handle_new_req`.

## 2. Add cache-pressure flush and TSU drain priority

- [x] 2.1 Modify `flash_sim/HIL.py::Cache_Manager.cache_write` and `flash_sim/HIL.py::Cache_Manager.write_flush` to flush all buffered cache entries to `flash_sim/FTL.py::Address_Mapping_Unit.translate_and_submit` when `Data_Cache` capacity is exhausted, and tag generated flush writes for later completion accounting.
- [x] 2.2 Modify `flash_sim/FTL.py::TSU.try_activate`, `flash_sim/FTL.py::TSU.try_read`, `flash_sim/FTL.py::TSU.try_write`, and any related scheduler state so cache-pressure drain mode prioritizes flushed `USER_WRITE` backlog over new `USER_READ` traffic until the backlog reaches zero.
- [x] 2.3 Modify `flash_sim/HIL.py::HIL._on_transaction_serviced` and the flush bookkeeping path so cache-pressure drain mode is cleared only after all flush-generated user writes have completed in the flash array.

## 3. Verification

- [x] 3.1 Extend `tests/test_data_cache.py` to cover pre-`WRITE_DATA` cache-entry registration, logical-address read hits, mixed hit/miss transactions within a single `READ_REQ`, and full-cache flush submission behavior.
- [x] 3.2 Add or update an end-to-end regression around `test_case/test_read_write.json` using `flash_sim/main.py` or the `flash_sim/cli.py` engine entrypoint so the read completes without AMU mapping-page failure.
- [x] 3.3 Run `python -m unittest tests.test_data_cache` and the chosen trace-driven simulator command, then confirm both the unit tests and the read-after-write trace pass with the new cache/drain behavior.
