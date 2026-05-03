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


class _DummyHIL:
    def __init__(self):
        class _DummyFTL:
            def __init__(self):
                self.address_mapping_unit = _DummyAMU()

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
