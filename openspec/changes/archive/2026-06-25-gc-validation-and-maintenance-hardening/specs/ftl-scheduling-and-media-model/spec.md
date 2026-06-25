## MODIFIED Requirements

### Requirement: GC maintenance preserves media bookkeeping

`Block_Manager` SHALL maintain free, valid, and invalid page counts, write frontiers, erase counts, and barriers for each physical plane and block. `GC_WL_Unit` MUST trigger when the free pool reaches or falls below its watermark, select only safe victims, submit the complete relocation chain, and return an erased block to the wear-aware free pool.

#### Scenario: GC relocation completes consistently

- **WHEN** a valid victim page completes `GC_READ -> GC_WRITE -> GC_ERASE`
- **THEN** mapping, page validity, block counters, barriers, physical storage, and maintenance counters MUST describe the same completed relocation

#### Scenario: Stale GC write cannot rewind a mapping

- **WHEN** the current LPA mapping no longer points to the `GC_WRITE` transaction's old address
- **THEN** the destination page MUST be invalidated and the current mapping MUST remain unchanged

### Requirement: Write backpressure uses physical-plane FIFO queues

Writes blocked by allocation thresholds SHALL wait in queues keyed by `(channel, chip, die, plane)`. Retry MUST consume only the submit-ready FIFO prefix, and a blocked head MUST prevent later transactions from bypassing it.

#### Scenario: Erase wakes only the matching physical plane

- **WHEN** a GC erase returns a block to one physical plane
- **THEN** only that plane's FIFO MAY be retried, while queues with the same local plane number on other dies or chips MUST remain unchanged

#### Scenario: Queue isolation preserves multi-die batching

- **WHEN** ready transactions for different dies of one chip reach TSU together
- **THEN** queue isolation MUST NOT prevent TSU from selecting them in the same scheduling batch

### Requirement: Delayed cache flushes preserve entry generations

The cache SHALL retain one in-flight or waiting flush per entry generation. Completion of an older generation MUST NOT discard data written into a newer generation.

#### Scenario: Repeated flush does not duplicate a retained write

- **WHEN** an entry generation already owns a waiting or in-flight flush
- **THEN** another flush pass MUST NOT create a duplicate transaction for that generation

### Requirement: Static wear leveling yields to capacity recovery

Static WL SHALL run only when the physical plane has no waiting writes, its free pool is above the GC watermark, and the actual safe source/destination wear gap reaches the configured threshold. Completion of a static-WL erase MUST NOT synchronously recurse into another static-WL chain.

#### Scenario: Static WL completes through real media components

- **WHEN** a safe cold block is selected for static WL
- **THEN** Engine, TSU, and PHY MUST complete the relocation and erase while preserving mapping, data, barriers, block state, and maintenance-event conservation
