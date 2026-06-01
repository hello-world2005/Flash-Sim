import unittest

from flash_sim.HIL import Cache_Manager, Data_Cache
from flash_sim.common import (
    Request,
    RequestType,
    Transaction,
    TransactionType,
    FlashAddress,
    SECTOR_PER_PAGE,
    INVALID_DATA,
)


class _DummyAMU:
    def __init__(self):
        self.submitted = []

    def translate_and_submit(self, req):
        self.submitted.append(req)


class _DummyTSU:
    def __init__(self):
        self.cache_pressure_drain_mode = False
        self.pending_cache_pressure_writes = 0

    def start_cache_pressure_drain(self, write_count):
        self.cache_pressure_drain_mode = True
        self.pending_cache_pressure_writes += write_count

    def finish_cache_pressure_write(self):
        if self.pending_cache_pressure_writes > 0:
            self.pending_cache_pressure_writes -= 1
        if self.pending_cache_pressure_writes == 0:
            self.cache_pressure_drain_mode = False


class _DummyHIL:
    def __init__(self):
        class _DummyFTL:
            def __init__(self):
                self.address_mapping_unit = _DummyAMU()
                self.tsu = _DummyTSU()

        self.ftl = _DummyFTL()


def _make_user_payload(indices_to_values):
    payload = [INVALID_DATA] * SECTOR_PER_PAGE
    bitmap = [0] * SECTOR_PER_PAGE
    for idx, value in indices_to_values.items():
        payload[idx] = value
        bitmap[idx] = 1
    return bitmap, payload


class TestDataCache(unittest.TestCase):
    def test_data_cache_capacity_must_align_with_cache_line(self):
        with self.assertRaises(ValueError):
            Data_Cache(cache_line_size=64, capacity=65)

    def test_register_write_request_creates_logical_entry_before_payload_arrives(self):
        hil = _DummyHIL()
        cm = Cache_Manager(hil)
        bitmap, _ = _make_user_payload({0: 11, 1: 22})
        write_req = Request(
            type=RequestType.WRITE,
            sq_id=0,
            transaction_list=[Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=5, bitmap=bitmap)],
        )
        cm.register_write_request(write_req)

        self.assertIn(5, cm.pending_user_pages)
        entry = cm.pending_user_pages[5]
        self.assertEqual(entry["bitmap"][0], 1)
        self.assertEqual(entry["bitmap"][1], 1)
        self.assertEqual(entry["ready_bitmap"][0], 0)
        self.assertEqual(entry["ready_bitmap"][1], 0)

        read_tr = Transaction(source_req=None, type=TransactionType.USER_READ, lpa=5, bitmap=bitmap)
        read_req = Request(type=RequestType.READ, transaction_list=[read_tr])
        cm.query_cache(read_req)

        self.assertEqual(read_req.transaction_list, [])
        self.assertTrue(read_tr.completed)
        self.assertEqual(read_tr.payload[0], INVALID_DATA)
        self.assertEqual(read_tr.payload[1], INVALID_DATA)

    def test_read_hit_returns_data_from_cache(self):
        hil = _DummyHIL()
        cm = Cache_Manager(hil)
        cm.cache = Data_Cache(cache_line_size=64, capacity=256)
        bitmap, payload = _make_user_payload({0: 11, 1: 22})
        write_req = Request(
            type=RequestType.WRITE,
            sq_id=0,
            transaction_list=[Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=5, bitmap=bitmap, payload=payload)],
        )
        cm.cache_write(write_req)

        read_tr = Transaction(source_req=None, type=TransactionType.USER_READ, lpa=5, bitmap=bitmap)
        read_req = Request(type=RequestType.READ, transaction_list=[read_tr])
        cm.query_cache(read_req)
        self.assertEqual(read_req.transaction_list, [])
        self.assertTrue(read_tr.completed)
        self.assertEqual(read_tr.payload[0], 11)
        self.assertEqual(read_tr.payload[1], 22)

    def test_mixed_hit_and_miss_only_forwards_uncached_transactions(self):
        hil = _DummyHIL()
        cm = Cache_Manager(hil)
        bitmap_hit, payload_hit = _make_user_payload({0: 11})
        write_req = Request(
            type=RequestType.WRITE,
            sq_id=0,
            transaction_list=[Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=5, bitmap=bitmap_hit, payload=payload_hit)],
        )
        cm.cache_write(write_req)

        hit_tr = Transaction(source_req=None, type=TransactionType.USER_READ, lpa=5, bitmap=bitmap_hit)
        miss_bitmap, _ = _make_user_payload({0: 33})
        miss_tr = Transaction(source_req=None, type=TransactionType.USER_READ, lpa=6, bitmap=miss_bitmap)
        read_req = Request(type=RequestType.READ, transaction_list=[hit_tr, miss_tr])

        cm.query_cache(read_req)

        self.assertTrue(hit_tr.completed)
        self.assertEqual(hit_tr.payload[0], 11)
        self.assertEqual(read_req.transaction_list, [miss_tr])
        self.assertFalse(miss_tr.completed)

    def test_cache_full_triggers_flush_then_buffers_new_write(self):
        hil = _DummyHIL()
        cm = Cache_Manager(hil)
        cm.cache = Data_Cache(cache_line_size=64, capacity=128)
        bitmap1, payload1 = _make_user_payload({0: 10, 1: 20})
        req1 = Request(
            type=RequestType.WRITE,
            sq_id=0,
            transaction_list=[Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=1, bitmap=bitmap1, payload=payload1)],
        )
        cm.cache_write(req1)
        self.assertEqual(len(cm.cache.lines), 2)

        bitmap2, payload2 = _make_user_payload({0: 30})
        req2 = Request(
            type=RequestType.WRITE,
            sq_id=0,
            transaction_list=[Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=2, bitmap=bitmap2, payload=payload2)],
        )
        cm.cache_write(req2)
        self.assertEqual(len(hil.ftl.address_mapping_unit.submitted), 1)
        self.assertEqual(len(cm.cache.lines), 1)
        self.assertIn(2, cm.pending_user_pages)
        self.assertTrue(hil.ftl.tsu.cache_pressure_drain_mode)
        self.assertEqual(hil.ftl.tsu.pending_cache_pressure_writes, 1)

    def test_static_write_flush_uses_amu_submission(self):
        hil = _DummyHIL()
        cm = Cache_Manager(hil)
        tr = Transaction(
            source_req=None,
            type=TransactionType.USER_STATIC_WRITE,
            lpa=123,
            address=FlashAddress(channel=0, chip=3, die=0, plane=0, sub_plane=0, page=-1),
            bitmap=[1],
            payload=[77],
        )
        req = Request(type=RequestType.STATIC_WRITE, sq_id=0, transaction_list=[tr])
        cm.cache_write(req)
        cm.write_flush()
        self.assertTrue(any(r.type == RequestType.STATIC_WRITE for r in hil.ftl.address_mapping_unit.submitted))


if __name__ == "__main__":
    unittest.main()
