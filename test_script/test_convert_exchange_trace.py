import gzip
import json
import tempfile
import unittest
from pathlib import Path

from validation.mqsim_flash.convert_exchange_trace import convert


class TestConvertExchangeTrace(unittest.TestCase):
    def test_mqsim_and_flashsim_outputs_both_use_64_byte_sectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "exchange.trace.csv.gz"
            rows = (
                "DiskRead,1000,p,t,c,128,192,x,0\n"
                "DiskWrite,1001,p,t,c,4096,4096,x,0\n"
            )
            with gzip.open(source, "wt", encoding="utf-8") as handle:
                handle.write(rows)
            mqsim = root / "trace.mqsim"
            flashsim = root / "trace.json"
            manifest = root / "manifest.json"

            stats = convert(
                source, mqsim, flashsim, manifest,
                member_pattern="*", member_limit=None, max_requests=None,
                disk_id=0, page_aligned_only=False, page_bytes=4096,
                source_time_unit="us", normalize_time=True,
            )

            self.assertEqual(stats.emitted, 2)
            mq_rows = [line.split() for line in mqsim.read_text().splitlines()]
            flash_rows = json.loads(flashsim.read_text())
            self.assertEqual(mq_rows[0][2:4], ["2", "3"])
            self.assertEqual(flash_rows[0]["start_lha"], 2)
            self.assertEqual(flash_rows[0]["size"], 3)
            data = json.loads(manifest.read_text())
            self.assertEqual(data["mqsim_sector_bytes"], 64)
            self.assertEqual(data["flashsim_sector_bytes"], 64)
            self.assertEqual(data["min_mqsim_lba_64"], 2)


if __name__ == "__main__":
    unittest.main()
