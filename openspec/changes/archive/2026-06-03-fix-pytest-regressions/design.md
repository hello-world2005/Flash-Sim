## Context

The current failures are not a single regression. They come from three separate contract drifts:

1. The repository now has two trace schemas in active use: a lightweight standalone simulator schema (`lba` / `address` / `block_address`) and an event-driven engine schema (`time` / `start_lha` / `size`). `parse_trace(...)` currently validates only the engine-style shape while `flash_sim.cli run` still feeds its output into `FlashSimulator`, which creates silent address corruption instead of a clean error.
2. Default geometry values are inconsistent across direct dataclass defaults, `FlashConfig.from_dict(...)`, README examples, and the expectations baked into geometry-facing tests. This makes even simple default-constructor behavior unreliable.
3. Some regression fixtures still assume older implicit setup behavior. `Block_Manager.preconditioning(...)` now depends on AMU-backed mapping context, and the read-after-write regression script no longer points at the trace it claims to test.

The change therefore needs a cross-cutting repair that touches tooling, configuration defaults, runtime fixtures, and regression tests without reopening already healthy request-flow behavior.

## Goals / Non-Goals

**Goals:**
- Re-establish a safe boundary between standalone simulator traces and engine traces.
- Make default geometry/config behavior consistent across constructors, serializers, CLI-visible reporting, and documentation.
- Keep AMU-backed preconditioning behavior explicit while repairing the tests and harnesses that now violate that contract.
- Produce a repair plan that lets `pytest` become trustworthy again as a regression signal.

**Non-Goals:**
- Redesigning HIL cache semantics, TSU policy, or NAND timing formulas.
- Migrating every trace file in the repository to one universal schema.
- Adding new storage features beyond what is needed to restore the current broken contracts.

## Decisions

### Decision: Use schema-aware trace handling instead of a single ambiguous parser contract

The repair should preserve both existing execution modes:
- standalone simulation via `FlashSimulator` and `flash_sim.cli run`
- event-driven execution via `Engine` and engine-oriented traces

The implementation should either:
- split validation/normalization into explicit standalone and engine paths, or
- keep a shared parser entrypoint but add unambiguous schema detection plus consumer-specific normalization

In either case, standalone execution must never silently reinterpret engine-only fields as zero-address standalone commands.

Alternatives considered:
- Convert all tooling to engine-style traces only.
  Rejected because the repository still exposes `FlashSimulator` and documents standalone traces as a supported interface.
- Keep the current shared parser and rely on downstream defaults.
  Rejected because it already causes silent logical-address corruption.

### Decision: Promote one default geometry baseline to the single source of truth

The repository should standardize on one documented baseline for default geometry values and make every constructor/serializer path derive from it. That source of truth should feed:
- `FlashGeometry()` defaults
- `FlashConfig()` defaults
- `FlashConfig.from_dict({})`
- `FlashConfig.to_dict()`
- README and geometry-facing tests

Alternatives considered:
- Keep the current small debugging defaults and rewrite docs/tests around them.
  Rejected because those values have already drifted from published behavior and unexpectedly shrink the default address space.
- Let `FlashGeometry` and `FlashConfig.from_dict(...)` keep different defaults.
  Rejected because it guarantees future regressions and makes serialization round-trips non-deterministic.

### Decision: Keep preconditioning's AMU dependency explicit and repair fixtures around it

`Block_Manager.preconditioning(...)` now materializes mapping pages, GTD entries, and warmed mapping state. That is logically tied to a real `Address_Mapping_Unit`, so the design should preserve an explicit AMU dependency rather than hide it.

Tests and standalone harnesses should therefore create the minimum valid AMU-backed topology when invoking preconditioning directly. Missing AMU context should keep failing fast with a clear error.

Alternatives considered:
- Auto-create a hidden AMU inside `Block_Manager.preconditioning(...)`.
  Rejected because it would duplicate runtime ownership rules and create mapping state that is disconnected from the caller's actual topology.
- Revert preconditioning to a PHY-only setup.
  Rejected because it would discard the mapping-state work the runtime now relies on.

### Decision: Replace the brittle scripted regression entry with an explicit trace target

The read-after-write regression should validate the intended behavior, not whichever trace `flash_sim/main.py` happens to hardcode today. The harness should either:
- invoke the engine directly with `test_case/test_read_write.json`, or
- make the script entrypoint accept an explicit trace path and use that in the test

Alternatives considered:
- Keep the hardcoded script and change cache limits or sample traces until the test passes.
  Rejected because that would hide the actual regression target and couple the test to unrelated sample-workload choices.

## Design Rationale

The through-line in all three failure groups is contract drift. The safest repair is not to relax tests until they pass, but to make each boundary explicit:
- one boundary between standalone and engine traces
- one source of truth for defaults
- one explicit dependency contract for preconditioning setup

That approach minimizes surprise for future contributors, keeps public behavior documented, and reduces the chance that a local debugging shortcut leaks into default repository behavior again.

## Risks / Trade-offs

- [Risk] Schema detection may remain ambiguous for partially populated commands. → Mitigation: reject mixed or incomplete schemas with explicit validation errors instead of guessing.
- [Risk] Restoring the documented default geometry may increase memory use or execution time relative to the temporary debugging defaults. → Mitigation: keep smaller geometries available only through explicit overrides in tests or local scripts.
- [Risk] Preconditioning tests will require more setup code once AMU becomes explicit. → Mitigation: add shared fixture helpers or compact local builders so the dependency stays obvious but not verbose.
- [Risk] Repairing `main.py`-based regression coverage without clarifying its role could leave two competing engine entrypoints. → Mitigation: prefer direct `Engine` or CLI-based regression paths and treat `main.py` as a thin script wrapper only.

## Migration Plan

1. Establish the shared default geometry constants and update constructor/serializer/doc consumers.
2. Refactor trace validation so standalone and engine paths are explicit and address-safe.
3. Repair scripted regression entrypoints to target the intended read-after-write trace.
4. Update preconditioning fixtures to construct explicit AMU-backed mapping context.
5. Re-run focused failing tests first, then the full `python -m pytest -v` suite.

## Open Questions

- Should the repository expose separate public helpers such as `parse_standalone_trace(...)` and `parse_engine_trace(...)`, or keep one public `parse_trace(...)` with stricter mode detection internally?
- Should `flash_sim/main.py` remain a supported regression script, or should tests move entirely to `Engine` / CLI entrypoints and treat `main.py` as developer convenience only?
