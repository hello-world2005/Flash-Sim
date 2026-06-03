## 1. Restore Standalone Trace Tooling

- [x] 1.1 Modify `flash_sim/parser.py::COMMAND_SCHEMA`, `flash_sim/parser.py::validate_command`, and `flash_sim/parser.py::parse_trace` to distinguish standalone simulator traces from engine traces instead of validating only the engine-style `time/start_lha/size` schema.
- [x] 1.2 Modify `flash_sim/cli.py::cmd_run` and any helper functions added in `flash_sim/parser.py` or `flash_sim/simulator.py` so standalone `flash-sim run` preserves non-zero logical addresses and rejects engine-only traces explicitly instead of silently coercing them.
- [x] 1.3 Modify `tests/test_parser.py` and any supporting standalone trace fixtures so they cover non-zero address preservation, operation-specific parameter preservation, and explicit failure on mixed standalone/engine schemas.

## 2. Normalize Default Flash Configuration

- [x] 2.1 Modify `flash_sim/config.py::FlashGeometry` and `flash_sim/config.py::FlashConfig` so direct constructors use the documented default geometry baseline instead of the current debugging-only geometry.
- [x] 2.2 Modify `flash_sim/config.py::FlashConfig.from_dict`, `flash_sim/config.py::FlashConfig.to_dict`, `flash_sim/chip.py`, and `README.md` so serialization, default address calculations, and documentation all share the same default geometry baseline.
- [x] 2.3 Modify `tests/test_config.py` and `tests/test_chip.py` only as needed to assert the repaired public defaults and the larger default address space consistently.

## 3. Repair Runtime Fixtures And Regression Harnesses

- [x] 3.1 Modify `tests/test_preconditioning.py` to construct an explicit `flash_sim.FTL.Address_Mapping_Unit` fixture and wire the minimum required `PHY` / block-manager dependencies before calling `flash_sim/FTL.py::Block_Manager.preconditioning`.
- [x] 3.2 Modify `flash_sim/main.py` or `tests/test_read_write_trace.py` so the scripted read-after-write regression executes `test_case/test_read_write.json` explicitly instead of the unrelated hardcoded `test_case/test_multi_write.json` sample workload.
- [x] 3.3 Modify `flash_sim/cli.py::cmd_run_engine` and `flash_sim/engine.py::Start_simulation` only as needed to remove the current engine-entry signature mismatch and keep scripted engine invocations aligned with the repaired regression path.
- [x] 3.4 Modify `tests/test_read_write_trace.py` and any supporting trace fixtures so the regression asserts successful read-after-write completion on the intended engine trace.

## 4. Verification

- [x] 4.1 Run targeted regression commands for `tests/test_parser.py`, `tests/test_config.py`, `tests/test_chip.py`, `tests/test_preconditioning.py`, and `tests/test_read_write_trace.py`, and confirm the previously failing cases pass.
- [x] 4.2 Run `python -m pytest -v` from the repository root and confirm the full suite passes without the current parser, config, preconditioning, or read-after-write failures.
