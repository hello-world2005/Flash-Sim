import unittest

from flash_sim.common import (
    MessageType,
    PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS,
    PCIE_PACKET_OVERHEAD_BYTES,
    REQUEST_STATUS_SUCCESS,
    Request,
    RequestType,
    SECTOR_SIZE_BYTES,
    Transaction,
    TransactionType,
)
from flash_sim.request_latency_report import (
    BASE_STAGE_NAMES,
    CSV_COLUMN_NAMES,
    RECONCILIATION_STAGE_NAMES,
    RequestLatencyRecorder,
)


def _csv_latency_sum(row):
    return sum(
        row[CSV_COLUMN_NAMES[index]]
        for index in (3, 4, 6, 7, 8, 9, 10)
    )


class TestRequestLatencyRecorder(unittest.TestCase):
    def _make_req(self, req_type=RequestType.READ, req_id="req-0"):
        return Request(
            type=req_type,
            lha_start=0,
            size=1,
            trace_index=0,
            trace_time=0,
            report_req_id=req_id,
        )

    def test_breakdown_reconciles_overlap_and_keeps_skipped_stages_zero(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.READ, "req-overlap")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_request_completed(req, 10)

        rec = recorder.requests[req.report_req_id]
        recorder._append_interval(rec, "intervals", "pcie_host_to_device", 0, 5)
        recorder._append_interval(rec, "intervals", "phy_array_exec", 3, 8)

        exported = recorder.export()["requests"][0]
        breakdown = exported["breakdown"]

        self.assertEqual(exported["total_latency"], 10)
        self.assertEqual(breakdown["pcie_host_to_device"], 5)
        self.assertEqual(breakdown["phy_array_exec"], 5)
        self.assertEqual(breakdown["overlap_latency"], 2)
        self.assertEqual(breakdown["untracked_latency"], 2)

        for stage in BASE_STAGE_NAMES:
            if stage not in {"pcie_host_to_device", "phy_array_exec"}:
                self.assertEqual(breakdown[stage], 0)
        for stage in RECONCILIATION_STAGE_NAMES:
            self.assertIn(stage, breakdown)

    def test_write_without_persistence_completion_is_marked_superseded(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.WRITE, "req-superseded")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_request_completed(req, 40)

        exported = recorder.export()["requests"][0]

        self.assertEqual(exported["host_total_latency"], 40)
        self.assertEqual(exported["persistence_status"], "superseded_in_cache")
        self.assertEqual(exported["persistence_total_latency"], 0)
        self.assertTrue(
            all(value == 0 for value in exported["persistence_breakdown"].values())
        )

    def test_background_flush_lineage_marks_write_as_persisted(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.WRITE, "req-persisted")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_request_completed(req, 30)

        flush_tr = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=0,
            report_origin_request_ids=[req.report_req_id],
        )
        recorder.note_tsu_enqueued(flush_tr, 30)
        recorder.note_tsu_dispatched(flush_tr, 40)
        recorder.note_phy_command_phase(
            [flush_tr],
            op_kind="write",
            start_time=40,
            finish_time=240,
            cmd_addr_time=100,
        )
        recorder.note_phy_array_phase(
            [flush_tr],
            op_kind="write",
            start_time=240,
            finish_time=640,
        )
        recorder.note_persistence_completed(flush_tr, 640)

        exported = recorder.export()["requests"][0]
        breakdown = exported["persistence_breakdown"]

        self.assertEqual(exported["persistence_status"], "persisted")
        self.assertEqual(exported["persistence_total_latency"], 640)
        self.assertEqual(breakdown["tsu_queue_wait"], 10)
        self.assertEqual(breakdown["phy_cmd_addr"], 100)
        self.assertEqual(breakdown["phy_data_in"], 100)
        self.assertEqual(breakdown["phy_array_exec"], 400)
        self.assertEqual(breakdown["phy_data_out"], 0)

    def test_csv_mapping_miss_read_rows_are_additive_before_data_return(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.READ, "req-read-mapping")
        req.size = 1
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_mapping_resolution(req, "mapping_read")

        rec = recorder.requests[req.report_req_id]
        recorder._append_interval(rec, "intervals", "host_dispatch", 0, 5)
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_host_to_device",
            5,
            15,
            {"message_type": MessageType.READ_REQ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "amu_mapping_wait",
            15,
            35,
            {"wait_key": "mapping-read"},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_cmd_addr",
            15,
            17,
            {"transaction_type": TransactionType.MAPPING_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_array_exec",
            17,
            25,
            {"transaction_type": TransactionType.MAPPING_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_data_out",
            25,
            35,
            {"transaction_type": TransactionType.MAPPING_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_cmd_addr",
            35,
            40,
            {"transaction_type": TransactionType.USER_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_array_exec",
            40,
            50,
            {"transaction_type": TransactionType.USER_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_data_out",
            50,
            60,
            {"transaction_type": TransactionType.USER_READ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_device_to_host",
            60,
            65,
            {"message_type": MessageType.REQ_COMP.value},
        )

        req.status = REQUEST_STATUS_SUCCESS
        recorder.note_request_completed(req, 65)

        row = recorder.export_csv_rows()[0]
        expected_payload_latency = (
            req.size * SECTOR_SIZE_BYTES + PCIE_PACKET_OVERHEAD_BYTES
        ) // PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        if (req.size * SECTOR_SIZE_BYTES + PCIE_PACKET_OVERHEAD_BYTES) % PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS:
            expected_payload_latency += 1

        self.assertEqual(row[CSV_COLUMN_NAMES[3]], 5)
        self.assertEqual(row[CSV_COLUMN_NAMES[4]], 10)
        self.assertEqual(row[CSV_COLUMN_NAMES[6]], 20)
        self.assertEqual(row[CSV_COLUMN_NAMES[7]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[8]], 15)
        self.assertEqual(row[CSV_COLUMN_NAMES[9]], 10)
        self.assertEqual(row[CSV_COLUMN_NAMES[10]], 5)
        self.assertEqual(row[CSV_COLUMN_NAMES[11]], expected_payload_latency)
        self.assertEqual(row[CSV_COLUMN_NAMES[2]] - row[CSV_COLUMN_NAMES[0]], _csv_latency_sum(row))

    def test_csv_write_row_uses_host_visible_completion_path(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.WRITE, "req-write-csv")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_mapping_resolution(req, "uncached_write")

        rec = recorder.requests[req.report_req_id]
        recorder._append_interval(rec, "intervals", "host_dispatch", 0, 10)
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_host_to_device",
            10,
            20,
            {"message_type": MessageType.WRITE_REQ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_device_to_host",
            20,
            25,
            {"message_type": MessageType.WRITE_DATA_REQ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_host_to_device",
            25,
            55,
            {"message_type": MessageType.WRITE_DATA.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_device_to_host",
            55,
            60,
            {"message_type": MessageType.REQ_COMP.value},
        )

        req.status = REQUEST_STATUS_SUCCESS
        recorder.note_request_completed(req, 60)

        flush_tr = Transaction(
            source_req=None,
            type=TransactionType.USER_WRITE,
            lpa=0,
            report_origin_request_ids=[req.report_req_id],
        )
        recorder.note_tsu_enqueued(flush_tr, 120)
        recorder.note_tsu_dispatched(flush_tr, 150)
        recorder.note_phy_command_phase(
            [flush_tr],
            op_kind="write",
            start_time=150,
            finish_time=350,
            cmd_addr_time=100,
        )
        recorder.note_phy_array_phase(
            [flush_tr],
            op_kind="write",
            start_time=350,
            finish_time=750,
        )
        recorder.note_persistence_completed(flush_tr, 750)

        row = recorder.export_csv_rows()[0]

        self.assertEqual(row[CSV_COLUMN_NAMES[1]], "WRITE")
        self.assertEqual(row[CSV_COLUMN_NAMES[2]], 60)
        self.assertEqual(row[CSV_COLUMN_NAMES[5]], "No")
        self.assertEqual(row[CSV_COLUMN_NAMES[3]], 10)
        self.assertEqual(row[CSV_COLUMN_NAMES[4]], 45)
        self.assertEqual(row[CSV_COLUMN_NAMES[6]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[7]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[8]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[9]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[10]], 5)
        self.assertEqual(row[CSV_COLUMN_NAMES[11]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[2]] - row[CSV_COLUMN_NAMES[0]], _csv_latency_sum(row))

    def test_csv_read_row_splits_status_and_response_payload_latency(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.READ, "req-read-csv")
        req.size = 2
        recorder.register_request(req, scheduled_time=10)
        recorder.note_req_init_executed(req, 12)
        recorder.note_mapping_resolution(req, "cmt_hit")

        rec = recorder.requests[req.report_req_id]
        recorder._append_interval(rec, "intervals", "host_dispatch", 12, 20)
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_host_to_device",
            20,
            40,
            {"message_type": MessageType.READ_REQ.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "pcie_device_to_host",
            90,
            110,
            {"message_type": MessageType.REQ_COMP.value},
        )

        req.status = REQUEST_STATUS_SUCCESS
        recorder.note_request_completed(req, 110)

        row = recorder.export_csv_rows()[0]
        expected_payload_latency = (
            req.size * SECTOR_SIZE_BYTES + PCIE_PACKET_OVERHEAD_BYTES
        ) // PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        if (req.size * SECTOR_SIZE_BYTES + PCIE_PACKET_OVERHEAD_BYTES) % PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS:
            expected_payload_latency += 1

        self.assertEqual(row[CSV_COLUMN_NAMES[0]], 12)
        self.assertEqual(row[CSV_COLUMN_NAMES[1]], "READ")
        self.assertEqual(row[CSV_COLUMN_NAMES[2]], 110)
        self.assertEqual(row[CSV_COLUMN_NAMES[5]], "Yes")
        self.assertEqual(row[CSV_COLUMN_NAMES[6]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[7]], 0)
        self.assertEqual(row[CSV_COLUMN_NAMES[10]], 20)
        self.assertEqual(row[CSV_COLUMN_NAMES[11]], expected_payload_latency)


if __name__ == "__main__":
    unittest.main()
