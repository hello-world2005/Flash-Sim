import unittest

from flash_sim.common import (
    FlashAddress,
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

    def test_direct_media_write_uses_host_completion_as_persistence(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.WRITE, "req-direct")
        req.cache_forced_bypass = True
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)

        rec = recorder.requests[req.report_req_id]
        recorder._append_interval(rec, "intervals", "tsu_queue_wait", 10, 30)
        recorder._append_interval(
            rec,
            "intervals",
            "phy_cmd_addr",
            30,
            40,
            {"transaction_type": TransactionType.USER_WRITE.value},
        )
        recorder._append_interval(
            rec,
            "intervals",
            "phy_array_exec",
            40,
            90,
            {"transaction_type": TransactionType.USER_WRITE.value},
        )
        recorder.note_request_completed(req, 100)

        exported = recorder.export()["requests"][0]

        self.assertEqual(exported["persistence_status"], "persisted")
        self.assertEqual(exported["persistence_origin"], "host_media_path")
        self.assertEqual(exported["persistence_completion_time"], 100)
        self.assertEqual(exported["persistence_total_latency"], exported["host_total_latency"])
        self.assertEqual(exported["persistence_breakdown"], exported["breakdown"])

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

    def test_maintenance_extrema_and_backpressure_match_recorded_events(self):
        recorder = RequestLatencyRecorder()
        tr = Transaction(source_req=None, type=TransactionType.USER_WRITE, lpa=9)
        plane0 = FlashAddress(
            channel=0,
            chip=0,
            die=0,
            plane=0,
            sub_plane=-1,
            page=-1,
        )
        plane1 = FlashAddress(
            channel=0,
            chip=0,
            die=1,
            plane=0,
            sub_plane=-1,
            page=-1,
        )

        recorder.note_backpressure_enqueue(tr, (0, 0, 0, 0), 10)
        recorder.note_backpressure_retry(
            tr,
            (0, 0, 0, 0),
            35,
            submitted=True,
        )
        recorder.note_plane_pool_snapshot(
            plane0,
            free_pool_count=5,
            wear_skew=2,
            waiting_write_count=1,
        )
        recorder.note_plane_pool_snapshot(
            plane0,
            free_pool_count=3,
            wear_skew=4,
            waiting_write_count=0,
        )
        recorder.note_plane_pool_snapshot(
            plane1,
            free_pool_count=7,
            wear_skew=1,
            waiting_write_count=2,
        )

        maintenance = recorder.export()["meta"]["maintenance"]

        self.assertEqual(maintenance["backpressure_enqueued"], 1)
        self.assertEqual(maintenance["backpressure_retried"], 1)
        self.assertEqual(maintenance["backpressure_wait_time"], 25)
        self.assertEqual(maintenance["current_waiting_writes"], 0)
        self.assertEqual(maintenance["max_waiting_writes"], 1)
        self.assertEqual(maintenance["min_free_pool"], 3)
        self.assertEqual(maintenance["max_wear_skew"], 4)
        self.assertEqual(
            maintenance["planes"]["ch0.chip0.die0.plane0"],
            {
                "min_free_pool": 3,
                "max_wear_skew": 4,
                "max_waiting_writes": 1,
            },
        )
        self.assertEqual(
            maintenance["planes"]["ch0.chip0.die1.plane0"]["max_waiting_writes"],
            2,
        )

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

    def test_json_mapping_resolution_counts_exports_cmt_hit(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.READ, "req-json-cmt")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_mapping_resolution(req, "cmt_hit")
        recorder.note_request_completed(req, 10)

        exported = recorder.export()["requests"][0]

        self.assertEqual(
            exported["mapping_resolution_counts"],
            {
                "cmt_hit": 1,
                "gmt_hit": 0,
                "metadata_hit": 0,
                "mapping_read": 0,
                "uncached_write": 0,
            },
        )

    def test_json_mapping_resolution_counts_exports_mapping_read(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.READ, "req-json-mapping")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_mapping_resolution(req, "mapping_read")
        recorder.note_request_completed(req, 10)

        exported = recorder.export()["requests"][0]

        self.assertEqual(exported["mapping_resolution_counts"]["mapping_read"], 1)
        self.assertEqual(exported["mapping_resolution_counts"]["cmt_hit"], 0)

    def test_json_mapping_resolution_counts_exports_zeroes_for_compute(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.COMPUTE, "req-json-compute")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_req_init_executed(req, 0)
        recorder.note_request_completed(req, 10)

        exported = recorder.export()["requests"][0]

        self.assertEqual(
            exported["mapping_resolution_counts"],
            {
                "cmt_hit": 0,
                "gmt_hit": 0,
                "metadata_hit": 0,
                "mapping_read": 0,
                "uncached_write": 0,
            },
        )

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

    def test_metadata_hit_is_recorded_as_non_cache_hit_mapping_resolution(self):
        recorder = RequestLatencyRecorder()
        req = self._make_req(RequestType.WRITE, "req-write-metadata-hit")
        recorder.register_request(req, scheduled_time=0)
        recorder.note_mapping_resolution(req, "metadata_hit")

        rec = recorder.requests[req.report_req_id]
        self.assertEqual(rec.mapping_resolution_counts["metadata_hit"], 1)
        self.assertEqual(recorder._cache_hit_value(rec), "No")


if __name__ == "__main__":
    unittest.main()
