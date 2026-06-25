## Context

The simulator allocates user PPAs before TSU dispatch while NAND reclamation completes asynchronously. Correctness therefore depends on explicit waiting/retry state, version-aware mapping updates, and accounting that remains consistent across AMU, Block Manager, TSU, PHY, and request reporting. This archive records the completed hardening work; temporary investigation notes and session-local debugging checklists are intentionally left outside the capability specs.

## Decisions

### Keep submit-time allocation and add explicit backpressure

Writes that cannot safely allocate remain in a FIFO queue keyed by `(channel, chip, die, plane)`. A reclaimed block retries only that physical plane. This keeps the current translation contract while preventing cross-die wakeups.

### Treat GC relocation as a conditional mapping update

A completed `GC_WRITE` updates the mapping only when the current mapping still points to `gc_old_address`. Stale or unresolvable relocation destinations are invalidated instead of recreating or rewinding mappings.

### Preserve cache generations across delayed flushes

Each retained flush identifies its cache-entry generation. Duplicate flush transactions are suppressed, and completion of an older generation cannot delete newer buffered data.

### Prioritize capacity recovery over static wear leveling

GC erase completion retries waiting writes first. Static WL runs only when no write is waiting, capacity is above the GC watermark, and the actual safe source/destination wear gap reaches the threshold. Static-WL erase completion does not recursively start another static-WL chain.

### Validate both invariants and sustained workloads

Focused tests cover branch-level invariants. The pressure matrix covers timing and working-set variants, overwrite races, low-invalid workloads, post-flush writes, and in-flight GC re-overwrites. Maintenance counters are checked for conservation rather than inferred from successful request completion alone.

## Trade-offs

- Waiting queues add explicit lifecycle state but avoid a larger late-PPA-binding redesign.
- Fixed-position metadata remains an intentional abstraction; dirty CMT promotion is used for persistence.
- Multi-die batching remains available, but this change does not add incremental die scheduling while a chip is busy.
- Pressure traces are validation assets, not workload realism claims; known realism gaps remain documented separately for future evaluation.
