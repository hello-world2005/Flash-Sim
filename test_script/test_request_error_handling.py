import unittest
from types import SimpleNamespace

from flash_sim.FTL import Address_Mapping_Unit
from flash_sim.HIL import HIL
from flash_sim.PHY import PHY, PageType
from flash_sim.common import (
    FlashAddress,
    INVALID_DATA,
    INVALID_MVPN,
    LPA_NO_PER_MAPPING_PAGE,
    MessageType,
    REQUEST_STATUS_ERROR,
    REQUEST_STATUS_SUCCESS,
    Request,
    RequestFailure,
    RequestType,
    SECTOR_PER_PAGE,
    STATIC_BASE_LHA,
    Transaction,
    TransactionType,
    WL_PER_STRING,
)
from flash_sim.PCIe_link import PCIe_message


class _RecordingPCIeLink:
    def __init__(self):
        self.sent = []

    def send(self, message, target):
        self.sent.append((message, target))


class _RecordingTSU:
    def __init__(self):
        self.submitted = []
        self.scheduled = False
        self.prepared = False
        self.phy = SimpleNamespace(_storage=[])

    def Prepare_trans_submission(self):
        self.prepared = True

    def Submit_trans(self, tr):
        self.submitted.append(tr)

    def Schedule(self):
        self.scheduled = True

    def start_cache_pressure_drain(self, write_count):
        pass

    def finish_cache_pressure_write(self):
        pass


class _RecordingFTL:
    def __init__(self):
        self.requests = []
        self.address_mapping_unit = SimpleNamespace(translate_and_submit=lambda req: None)
        self.tsu = _RecordingTSU()

    def Validate_construction(self):
        pass

    def handle_new_req(self, req):
        self.requests.append(req)


def _make_hil():
    host = SimpleNamespace(
        num_of_queues=8,
        pcie_link=_RecordingPCIeLink(),
        queue_ptrs=SimpleNamespace(cq_tails=[0] * 8),
    )
    device = SimpleNamespace()
    hil = HIL(name="HIL", host=host, device=device)
    hil.ftl = _RecordingFTL()
    return hil, host, hil.ftl


class TestRequestErrorHandling(unittest.TestCase):
    def test_hil_complete_request_sends_success_payload(self):
        hil, host, _ = _make_hil()
        req = Request(type=RequestType.READ, sq_id=0, lha_start=4, size=1)

        hil._complete_request(req)

        self.assertEqual(req.status, REQUEST_STATUS_SUCCESS)
        self.assertIsNone(req.error_message)
        self.assertTrue(req.completion_sent)
        self.assertEqual(len(host.pcie_link.sent), 2)
        data_message, data_target = host.pcie_link.sent[0]
        self.assertIs(data_target, host)
        self.assertEqual(data_message.type, MessageType.READ_RES_SEND_BACK)
        message, target = host.pcie_link.sent[1]
        self.assertIs(target, host)
        self.assertEqual(message.type, MessageType.REQ_COMP)
        self.assertEqual(message.payload["status"], REQUEST_STATUS_SUCCESS)
        self.assertIsNone(message.payload["error_message"])

    def test_hil_invalid_compute_request_returns_error_without_fetch_or_ftl_submit(self):
        hil, host, ftl = _make_hil()
        req = Request(type=RequestType.COMPUTE, sq_id=0, lha_start=0, size=1, selected_wl=0)

        hil.receive_pcie_message(
            PCIe_message(type=MessageType.COMPUTE_REQ, payload={"req": req})
        )

        self.assertEqual(req.status, REQUEST_STATUS_ERROR)
        self.assertIn("static area", req.error_message)
        self.assertTrue(req.completion_sent)
        self.assertEqual(ftl.requests, [])
        self.assertEqual(len(host.pcie_link.sent), 1)
        message, _ = host.pcie_link.sent[0]
        self.assertEqual(message.type, MessageType.REQ_COMP)
        self.assertEqual(message.payload["status"], REQUEST_STATUS_ERROR)
        self.assertIn("static area", message.payload["error_message"])

    def test_hil_compute_request_requires_selected_wl_before_data_fetch(self):
        hil, host, ftl = _make_hil()
        req = Request(type=RequestType.COMPUTE, sq_id=0, lha_start=STATIC_BASE_LHA, size=1)

        hil.receive_pcie_message(
            PCIe_message(type=MessageType.COMPUTE_REQ, payload={"req": req})
        )

        self.assertEqual(req.status, REQUEST_STATUS_ERROR)
        self.assertIn("selected_wl", req.error_message)
        self.assertEqual(ftl.requests, [])
        self.assertEqual(len(host.pcie_link.sent), 1)

    def test_hil_compute_selected_wl_range_is_half_open(self):
        hil, _, _ = _make_hil()
        for selected_wl in (0, WL_PER_STRING - 1):
            req = Request(
                type=RequestType.COMPUTE,
                lha_start=STATIC_BASE_LHA,
                size=1,
                selected_wl=selected_wl,
            )
            hil._validate_request_domain(req)

        for selected_wl in (-1, WL_PER_STRING):
            req = Request(
                type=RequestType.COMPUTE,
                lha_start=STATIC_BASE_LHA,
                size=1,
                selected_wl=selected_wl,
            )
            with self.assertRaisesRegex(RequestFailure, "selected_wl"):
                hil._validate_request_domain(req)

    def test_hil_invalid_write_to_static_does_not_register_cache_entry(self):
        hil, host, _ = _make_hil()
        req = Request(type=RequestType.WRITE, sq_id=0, lha_start=STATIC_BASE_LHA, size=1)

        hil.receive_pcie_message(
            PCIe_message(type=MessageType.WRITE_REQ, payload={"req": req})
        )

        self.assertEqual(req.status, REQUEST_STATUS_ERROR)
        self.assertEqual(hil.cache_manager.pending_user_pages, {})
        self.assertEqual(len(host.pcie_link.sent), 1)
        self.assertEqual(host.pcie_link.sent[0][0].type, MessageType.REQ_COMP)

    def test_amu_unmapped_read_raises_request_failure_without_queueing_waiter(self):
        amu = Address_Mapping_Unit()
        amu.tsu = _RecordingTSU()
        amu.block_manager = SimpleNamespace(has_pending_host_write=lambda lpa: False)
        req = Request(type=RequestType.READ, sq_id=0)
        tr = Transaction(
            source_req=req,
            type=TransactionType.USER_READ,
            lpa=0,
            bitmap=[1] + [0] * (SECTOR_PER_PAGE - 1),
        )
        req.transaction_list = [tr]

        with self.assertRaisesRegex(RequestFailure, "non-existing mapping page"):
            amu.translate_and_submit(req)

        self.assertEqual(amu.waiting_for_mapping_trans[0], [])
        self.assertEqual(amu.tsu.submitted, [])

    def test_phy_invalid_sector_for_host_read_becomes_request_failure(self):
        phy = PHY()
        req = Request(type=RequestType.READ, sq_id=0)
        tr = Transaction(
            source_req=req,
            type=TransactionType.USER_READ,
            lpa=7,
            address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=0, page=0),
            bitmap=[1] + [0] * (SECTOR_PER_PAGE - 1),
        )
        pd = phy._storage[0][0][0][0][0][0]
        pd.function = PageType.USER
        pd.lpa = 7
        pd.mvpn = INVALID_MVPN
        pd.valid_bitmap = [1] * SECTOR_PER_PAGE
        pd.data = [INVALID_DATA] + [11] * (SECTOR_PER_PAGE - 1)

        with self.assertRaisesRegex(RequestFailure, "invalid sector"):
            phy._read_from_storage(tr)

    def test_phy_invalid_mapping_slot_for_host_mapping_read_becomes_request_failure(self):
        phy = PHY()
        req = Request(type=RequestType.READ, sq_id=0)
        tr = Transaction(
            source_req=req,
            type=TransactionType.MAPPING_READ,
            mvpn=1,
            address=FlashAddress(channel=0, chip=0, die=0, plane=0, sub_plane=0, page=0),
            bitmap=[1] + [0] * (LPA_NO_PER_MAPPING_PAGE - 1),
        )
        pd = phy._storage[0][0][0][0][0][0]
        pd.function = PageType.MAPPING
        pd.lpa = -1
        pd.mvpn = 1
        pd.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        pd.data = [0] * LPA_NO_PER_MAPPING_PAGE

        with self.assertRaisesRegex(RequestFailure, "invalid lpa"):
            phy._read_from_storage(tr)


if __name__ == "__main__":
    unittest.main()
