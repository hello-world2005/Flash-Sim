# -*- coding: utf-8 -*-
from collections import deque
from math import ceil
from typing import TYPE_CHECKING, Any

from dataclasses import dataclass
from .common import (
    EventType,
    MessageType,
    PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS,
    PCIE_NVME_CQ_ENTRY_BYTES,
    PCIE_NVME_SQ_ENTRY_BYTES,
    PCIE_PACKET_OVERHEAD_BYTES,
    PCIE_TLP_MAX_PAYLOAD_BYTES,
    PCIE_TLP_PACKET_OVERHEAD_BYTES,
    REQUEST_LATENCY_RECORDER,
    SECTOR_SIZE_BYTES,
)

if TYPE_CHECKING:
    from .engine import Engine

@dataclass
class PCIe_message:
    type: MessageType
    payload: dict[str, Any]

    def __str__(self) -> str:
        lines = ["PCIe_message:", f"  type:    {self.type}", "  payload:"]
        for k, v in self.payload.items():
            if hasattr(v, "__str__") and "\n" in str(v):
                lines.append(f"    {k}:")
                for line in str(v).strip().split("\n"):
                    lines.append("      " + line)
            else:
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)


class PCIe_link:
    def __init__(self, host, device):
        self._construction_valid: bool = False
        self.host = host
        self.device = device
        self.engine: Engine  # 在 Engine 中注入后生效
        self.host_to_device_queue = deque()
        self.device_to_host_queue = deque()

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.host is not None, "PCIe_link host is not set"
        assert self.device is not None, "PCIe_link device is not set"
        assert self.engine is not None, "PCIe_link engine is not set"
        assert self.host_to_device_queue is not None, "PCIe_link host_to_device_queue is not set"
        assert self.device_to_host_queue is not None, "PCIe_link device_to_host_queue is not set"
        self._construction_valid = True

    def send(self, message, target):
        estimated_latency = self.estimate_latency(message)
        transfer_bytes = self._estimate_transfer_bytes(message)
        recorder = REQUEST_LATENCY_RECORDER()
        if target == self.device:
            self.host_to_device_queue.append(message)
            if recorder is not None:
                recorder.note_pcie_enqueued(
                    message,
                    "host_to_device",
                    self.engine.current_time,
                    transfer_bytes,
                )
            if len(self.host_to_device_queue) == 1:
                estimated_finish_time = self.engine.current_time + estimated_latency
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.device.hil}, estimated_finish_time)
        elif target == self.host:
            self.device_to_host_queue.append(message)
            if recorder is not None:
                recorder.note_pcie_enqueued(
                    message,
                    "device_to_host",
                    self.engine.current_time,
                    transfer_bytes,
                )
            if len(self.device_to_host_queue) == 1:
                estimated_finish_time = self.engine.current_time + estimated_latency
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.host}, estimated_finish_time)

    def estimate_latency(self, message):
        transfer_bytes = self._estimate_transfer_bytes(message)
        bandwidth = PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        if bandwidth <= 0:
            raise ValueError("PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS must be positive")
        return ceil(transfer_bytes / bandwidth)

    def _estimate_transfer_bytes(self, message) -> int:
        """Return wire bytes using MQSim's PCIe/NVMe transfer model.

        Flash-Sim carries the request object directly in one message, while
        MQSim models doorbell write + SQ read request + SQ entry DMA.  Charge
        their aggregate wire bytes to the request message.  Payload and CQ
        transfers use the same 128-B TLP and 28-B per-packet overhead as
        MQSim.  *_RECEIVED messages are internal queue-release notifications
        and have no MQSim PCIe counterpart, so they consume no link time.
        """
        message_type = getattr(message, "type", None)
        if message_type in (
            MessageType.READ_REQ,
            MessageType.WRITE_REQ,
        ):
            doorbell_bytes = PCIE_TLP_PACKET_OVERHEAD_BYTES + 2
            sq_read_request_bytes = PCIE_TLP_PACKET_OVERHEAD_BYTES + 4
            sq_entry_bytes = self._tlp_wire_bytes(PCIE_NVME_SQ_ENTRY_BYTES)
            return doorbell_bytes + sq_read_request_bytes + sq_entry_bytes
        if message_type in (
            MessageType.WRITE_DATA_REQ,
        ):
            return PCIE_TLP_PACKET_OVERHEAD_BYTES + 4
        if message_type in (
            MessageType.WRITE_DATA_RECEIVED,
            MessageType.READ_REQ_RECEIVED,
        ):
            return 0
        if message_type == MessageType.REQ_COMP:
            return self._tlp_wire_bytes(PCIE_NVME_CQ_ENTRY_BYTES)

        payload = getattr(message, "payload", None)
        user_data_bytes = 0
        if isinstance(payload, dict) and "data" in payload:
            user_data_bytes = self._estimate_user_data_bytes(payload["data"])
            return self._tlp_wire_bytes(user_data_bytes)
        return PCIE_PACKET_OVERHEAD_BYTES

    @staticmethod
    def _tlp_wire_bytes(payload_bytes: int) -> int:
        if payload_bytes <= 0:
            return 0
        packet_count = ceil(payload_bytes / PCIE_TLP_MAX_PAYLOAD_BYTES)
        return payload_bytes + packet_count * PCIE_TLP_PACKET_OVERHEAD_BYTES

    def _estimate_user_data_bytes(self, data) -> int:
        if data is None:
            return 0
        if isinstance(data, (bytes, bytearray, memoryview)):
            return len(data)
        if not hasattr(data, "__len__"):
            raise TypeError(f"Unsupported PCIe payload data type: {type(data).__name__}")
        return len(data) * SECTOR_SIZE_BYTES

    def Register_sim_event(self, event_type, target, param, scheduled_time):
        self.engine.Register_event(event_type, target, param, scheduled_time)

    def execute(self, event):
        from .common import log_execute_event
        log_execute_event(self.__class__.__name__, event)
        assert event.type == EventType.DELIVER
        target = event.param["target"]
        message = None
        if target == self.device.hil:
            message = self.host_to_device_queue.popleft()
        elif target == self.host:
            message = self.device_to_host_queue.popleft()
        else:
            raise ValueError(f"[PCIe_link] <execute> unexpected target: {target}")
        event.param["message"] = message
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_pcie_delivered(message, self.engine.current_time)
        target.execute(event)
        if target == self.device.hil:
            if len(self.host_to_device_queue) > 0:
                new_message = self.host_to_device_queue[0]
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.device.hil}, self.engine.current_time + self.estimate_latency(new_message))
        elif target == self.host:
            if len(self.device_to_host_queue) > 0:
                new_message = self.device_to_host_queue[0]
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.host}, self.engine.current_time + self.estimate_latency(new_message))
