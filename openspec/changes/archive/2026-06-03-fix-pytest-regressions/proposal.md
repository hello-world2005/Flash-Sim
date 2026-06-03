## Why

The current `python -m pytest` baseline is broken because several public and semi-public contracts have drifted apart: standalone trace parsing no longer matches documented `FlashSimulator` usage, default geometry values are inconsistent across constructors and documentation, and some regression fixtures no longer initialize the runtime the way the implementation now requires. We need a single repair pass now so contributors can trust the test suite as a signal again.

## What Changes

- Restore a consistent trace-ingestion contract for standalone simulator tooling so `parse_trace(...)`, `flash_sim.cli run`, `FlashSimulator`, and the documented JSON formats agree on how commands are represented, without silently rewriting addresses.
- Define a single documented default flash configuration baseline and make `FlashGeometry()`, `FlashConfig()`, `FlashConfig.from_dict(...)`, serialization helpers, and geometry-facing tests all agree on that baseline.
- Clarify the preconditioning caller contract around `Address_Mapping_Unit` initialization and update regression fixtures to construct the minimum valid runtime dependencies instead of relying on removed implicit behavior.
- Repair the read-after-write regression harness so its scripted entrypoint exercises the intended `test_case/test_read_write.json` path instead of an unrelated oversized write trace.
- Add focused regression coverage for standalone trace compatibility, default geometry round-trips, preconditioning setup, and scripted read-after-write execution.

## Capabilities

### New Capabilities
- `default-flash-configuration`: Define the public default flash geometry/configuration contract and require consistency across constructors, serialization, CLI reporting, and documentation.

### Modified Capabilities
- `simulator-tooling`: Trace parsing and standalone CLI execution requirements must change so lightweight simulator traces and engine traces are handled consistently and without silent address corruption.
- `ftl-scheduling-and-media-model`: The preconditioning requirement must make AMU-backed mapping initialization an explicit caller contract for both runtime startup and direct test fixtures.

## Non-goals

- Reworking HIL cache semantics, TSU scheduling policy, or NAND timing equations beyond what is needed to restore the current failing regressions.
- Changing the logical meaning of `READ`, `WRITE`, `SEARCH`, `COMPUTE`, or `STATIC_WRITE` requests in the healthy paths that already pass today.
- Replacing the event-driven engine architecture or redesigning the OpenSpec capability layout outside the three capabilities listed above.

## Impact

- In scope modules/functions: `flash_sim/parser.py`, `flash_sim/cli.py`, `flash_sim/simulator.py`, `flash_sim/config.py`, `flash_sim/chip.py`, `flash_sim/FTL.py::Block_Manager.preconditioning`, `flash_sim/main.py`, and the failing regression tests in `tests/test_parser.py`, `tests/test_config.py`, `tests/test_chip.py`, `tests/test_preconditioning.py`, and `tests/test_read_write_trace.py`.
- In scope docs: `README.md` and any developer-facing notes that currently describe the default config or standalone trace format.
- Proposed regression targets: a standalone `flash-sim run` trace that preserves non-zero logical addresses, a default-config round-trip assertion across constructor and `to_dict()/from_dict()`, a preconditioning test with an explicit AMU fixture, and a scripted read-after-write trace that completes successfully from `test_case/test_read_write.json`.
- Out of scope modules: `flash_sim/HIL.py` request-cache behavior, `flash_sim/PHY.py` media timing behavior, and unrelated benchmark or visualization workflows except where they depend on the repaired parser/config contracts.
