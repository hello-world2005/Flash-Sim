import subprocess
import sys
import unittest
from pathlib import Path


class TestReadWriteTrace(unittest.TestCase):
    def test_main_trace_read_after_write_completes_without_mapping_error(self):
        repo_root = Path(__file__).resolve().parents[1]
        log_path = repo_root / "output" / "test_read_write.log"
        proc = subprocess.run(
            [sys.executable, "flash_sim/main.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, msg=combined)
        self.assertNotIn("Read request accessing non-existing mapping page", combined)
        self.assertNotIn("Error:", combined)
        self.assertIn("RequestType.READ", combined)
        self.assertTrue(log_path.exists(), msg="expected read/write regression log to be generated")


if __name__ == "__main__":
    unittest.main()
