import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from flash_sim.PHY import PageType
from flash_sim.common import INVALID_DATA, SECTOR_PER_PAGE
from flash_sim.engine import Engine


REPO_ROOT = Path(__file__).resolve().parents[1]
READ_ERROR_LHA = 106688


def _run_engine_with_trace(trace_path: Path, mutate_after_preconditioning=None) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        engine = Engine()
        engine.Validate_construction()
        engine.device.ftl.block_manager.preconditioning(
            phy=engine.device.ftl.tsu.phy,
            amu=engine.device.ftl.address_mapping_unit,
        )
        if mutate_after_preconditioning is not None:
            mutate_after_preconditioning(engine)
        engine.Initialize_event_queue(str(trace_path))
        engine.Run()
    return buf.getvalue()


def _corrupt_mapped_sector(engine: Engine, target_lha: int) -> None:
    target_lpa = target_lha // SECTOR_PER_PAGE
    target_sector = target_lha % SECTOR_PER_PAGE
    for channel in engine.device.ftl.tsu.phy._storage:
        for chip in channel:
            for die in chip:
                for plane in die:
                    for block in plane:
                        for page in block:
                            if page.function == PageType.USER and page.lpa == target_lpa:
                                page.data[target_sector] = INVALID_DATA
                                return
    raise AssertionError(f"Could not find mapped page for lha {target_lha}")


class TestInvalidRequestErrorsTrace(unittest.TestCase):
    def test_invalid_domain_requests_complete_with_error_logs(self):
        output = _run_engine_with_trace(
            REPO_ROOT / "test_case" / "test_invalid_domain_requests.json"
        )

        self.assertNotIn("Traceback", output)
        self.assertIn("SEARCH request must stay in static area", output)
        self.assertIn("COMPUTE request must stay in static area", output)
        self.assertIn(
            "WRITE request must stay in random-access area; use STATIC_WRITE for static-area writes",
            output,
        )
        self.assertGreaterEqual(output.count("status=ERROR"), 3)

    def test_unmapped_read_completes_with_error_log(self):
        output = _run_engine_with_trace(
            REPO_ROOT / "test_case" / "test_unmapped_read_error.json",
        )

        self.assertNotIn("Traceback", output)
        self.assertIn("Read request accessing non-existing mapping page", output)
        self.assertGreaterEqual(output.count("status=ERROR"), 1)

    def test_invalid_sector_read_completes_with_error_log(self):
        output = _run_engine_with_trace(
            REPO_ROOT / "test_case" / "test_invalid_sector_read_error.json",
            mutate_after_preconditioning=lambda engine: _corrupt_mapped_sector(
                engine, READ_ERROR_LHA
            ),
        )

        self.assertNotIn("Traceback", output)
        self.assertIn("[PHY] <_read_from_storage> accessing invalid sector in user page!", output)
        self.assertGreaterEqual(output.count("status=ERROR"), 1)


if __name__ == "__main__":
    unittest.main()
