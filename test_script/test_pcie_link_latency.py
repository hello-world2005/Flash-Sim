import unittest

from flash_sim.common import (
    EventType,
    MessageType,
    PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS,
    PCIE_NVME_SQ_ENTRY_BYTES,
    PCIE_TLP_MAX_PAYLOAD_BYTES,
    PCIE_TLP_PACKET_OVERHEAD_BYTES,
    Request,
    RequestType,
    SECTOR_SIZE_BYTES,
    SimEvent,
)
from flash_sim.PCIe_link import PCIe_link, PCIe_message


class _DummyEngine:
    def __init__(self):
        self.current_time = 0
        self.registered_events = []

    def Register_event(self, event_type, target, param, scheduled_time):
        self.registered_events.append(
            {
                "event_type": event_type,
                "target": target,
                "param": param,
                "scheduled_time": scheduled_time,
            }
        )


class _DummyEndpoint:
    def __init__(self, name):
        self.name = name
        self.received_messages = []

    def execute(self, event):
        self.received_messages.append(
            {
                "time": event.time,
                "message": event.param["message"],
            }
        )


class _DummyDevice:
    def __init__(self):
        self.hil = _DummyEndpoint("device.hil")


class TestPCIeLinkLatency(unittest.TestCase):
    def _make_link(self):
        host = _DummyEndpoint("host")
        device = _DummyDevice()
        link = PCIe_link(host, device)
        link.engine = _DummyEngine()
        return link, host, device

    def _make_req(self, req_type=RequestType.WRITE, size=1):
        return Request(type=req_type, lha_start=0, size=size)

    def _execute_registered_event(self, link, event):
        link.engine.current_time = event["scheduled_time"]
        link.execute(
            SimEvent(
                type=EventType.DELIVER,
                target=link,
                time=link.engine.current_time,
                param={"target": event["param"]["target"]},
            )
        )

    def test_request_message_uses_mqsim_nvme_submission_cost(self):
        link, _, _ = self._make_link()
        req = self._make_req(RequestType.READ)
        msg = PCIe_message(MessageType.READ_REQ, payload={"req": req})

        wire_bytes = (
            PCIE_TLP_PACKET_OVERHEAD_BYTES + 2
            + PCIE_TLP_PACKET_OVERHEAD_BYTES + 4
            + PCIE_NVME_SQ_ENTRY_BYTES + PCIE_TLP_PACKET_OVERHEAD_BYTES
        )
        expected_latency = -(-wire_bytes // PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS)

        self.assertEqual(link.estimate_latency(msg), expected_latency)

    def test_nvme_command_submission_uses_three_causal_bidirectional_phases(self):
        link, host, device = self._make_link()
        req = self._make_req(RequestType.READ)
        msg = PCIe_message(MessageType.READ_REQ, payload={"req": req})

        link.send(msg, link.device)

        doorbell_event = link.engine.registered_events[0]
        self.assertEqual(doorbell_event["scheduled_time"], 8)
        self.assertIs(doorbell_event["param"]["target"], device.hil)
        self._execute_registered_event(link, doorbell_event)
        self.assertEqual(device.hil.received_messages, [])

        sq_read_event = link.engine.registered_events[1]
        self.assertEqual(sq_read_event["scheduled_time"], 16)
        self.assertIs(sq_read_event["param"]["target"], host)
        self._execute_registered_event(link, sq_read_event)
        self.assertEqual(host.received_messages, [])

        sq_entry_event = link.engine.registered_events[2]
        self.assertEqual(sq_entry_event["scheduled_time"], 41)
        self.assertIs(sq_entry_event["param"]["target"], device.hil)
        self._execute_registered_event(link, sq_entry_event)

        self.assertEqual(
            device.hil.received_messages,
            [{"time": 41, "message": msg}],
        )

    def test_two_nvme_commands_pipeline_across_both_directions(self):
        link, _, device = self._make_link()
        first = PCIe_message(
            MessageType.READ_REQ,
            payload={"req": self._make_req(RequestType.READ)},
        )
        second = PCIe_message(
            MessageType.READ_REQ,
            payload={"req": self._make_req(RequestType.READ)},
        )

        link.send(first, link.device)
        link.send(second, link.device)

        # Doorbell A (8), SQ read A (16), doorbell B (16), SQ read B (24),
        # SQE A (41), SQE B (66).  The second command completes at 66 ns,
        # rather than consuming another aggregated 41 ns H2D slot.
        for event_index in range(6):
            self._execute_registered_event(link, link.engine.registered_events[event_index])

        self.assertEqual(
            [item["time"] for item in device.hil.received_messages],
            [41, 66],
        )
        self.assertEqual(
            [item["message"] for item in device.hil.received_messages],
            [first, second],
        )

    def test_data_message_latency_scales_with_payload_size(self):
        link, _, _ = self._make_link()
        req = self._make_req(RequestType.WRITE)
        small_msg = PCIe_message(
            MessageType.WRITE_DATA,
            payload={"req": req, "data": [1]},
        )
        large_msg = PCIe_message(
            MessageType.WRITE_DATA,
            payload={"req": req, "data": [1, 2, 3, 4]},
        )

        small_payload = SECTOR_SIZE_BYTES
        large_payload = 4 * SECTOR_SIZE_BYTES
        expected_small = -(
            -(small_payload + PCIE_TLP_PACKET_OVERHEAD_BYTES)
            // PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        )
        large_packets = -(-large_payload // PCIE_TLP_MAX_PAYLOAD_BYTES)
        expected_large = -(
            -(large_payload + large_packets * PCIE_TLP_PACKET_OVERHEAD_BYTES)
            // PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        )

        self.assertEqual(link.estimate_latency(small_msg), expected_small)
        self.assertEqual(link.estimate_latency(large_msg), expected_large)
        self.assertGreater(link.estimate_latency(large_msg), link.estimate_latency(small_msg))

    def test_host_to_device_queue_stays_serialized_with_message_specific_delays(self):
        link, _, device = self._make_link()
        first_req = self._make_req(RequestType.WRITE, size=1)
        second_req = self._make_req(RequestType.WRITE, size=4)
        first_msg = PCIe_message(
            MessageType.WRITE_DATA,
            payload={"req": first_req, "data": [6]},
        )
        second_msg = PCIe_message(
            MessageType.WRITE_DATA,
            payload={"req": second_req, "data": [7, 8, 9, 10]},
        )

        link.send(first_msg, link.device)
        link.send(second_msg, link.device)

        self.assertEqual(len(link.engine.registered_events), 1)
        first_event = link.engine.registered_events[0]
        self.assertEqual(first_event["scheduled_time"], link.estimate_latency(first_msg))

        link.engine.current_time = first_event["scheduled_time"]
        link.execute(
            SimEvent(
                type=EventType.DELIVER,
                target=link,
                time=link.engine.current_time,
                param={"target": link.device.hil},
            )
        )

        self.assertEqual(device.hil.received_messages[0]["message"], first_msg)
        self.assertEqual(len(link.engine.registered_events), 2)
        second_event = link.engine.registered_events[1]
        self.assertEqual(
            second_event["scheduled_time"],
            first_event["scheduled_time"] + link.estimate_latency(second_msg),
        )

    def test_bidirectional_first_messages_schedule_independently(self):
        link, host, _ = self._make_link()
        write_req = self._make_req(RequestType.WRITE, size=2)
        completion_req = self._make_req(RequestType.READ, size=2)
        host_to_device = PCIe_message(
            MessageType.WRITE_DATA,
            payload={"req": write_req, "data": [1, 2]},
        )
        device_to_host = PCIe_message(
            MessageType.REQ_COMP,
            payload={"req": completion_req, "status": "SUCCESS", "error_message": None},
        )

        link.send(host_to_device, link.device)
        link.send(device_to_host, link.host)

        self.assertEqual(len(link.engine.registered_events), 2)
        self.assertEqual(
            link.engine.registered_events[0]["scheduled_time"],
            link.estimate_latency(host_to_device),
        )
        self.assertEqual(
            link.engine.registered_events[1]["scheduled_time"],
            link.estimate_latency(device_to_host),
        )
        self.assertEqual(host.received_messages, [])

    def test_internal_queue_release_bypasses_busy_device_to_host_fifo(self):
        link, host, _ = self._make_link()
        req = self._make_req(RequestType.READ, size=64)
        read_data = PCIe_message(
            MessageType.READ_RES_SEND_BACK,
            payload={"req": req, "data": [1] * 64},
        )
        queue_release = PCIe_message(
            MessageType.READ_REQ_RECEIVED,
            payload={"sq_id": 0},
        )

        link.send(read_data, link.host)
        link.send(queue_release, link.host)

        self.assertEqual(list(link.device_to_host_queue), [read_data])
        self.assertEqual(len(link.engine.registered_events), 2)
        self.assertEqual(
            link.engine.registered_events[0]["scheduled_time"],
            link.estimate_latency(read_data),
        )
        self.assertEqual(link.engine.registered_events[1]["scheduled_time"], 0)
        self.assertIs(link.engine.registered_events[1]["target"], host)
        self.assertEqual(
            link.engine.registered_events[1]["param"]["message"],
            queue_release,
        )


if __name__ == "__main__":
    unittest.main()
