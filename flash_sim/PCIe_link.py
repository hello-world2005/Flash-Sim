# -*- coding: utf-8 -*-
from collections import deque
from math import ceil
from typing import TYPE_CHECKING, Any

from dataclasses import dataclass, field
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
    _nvme_command_phase: str | None = field(default=None, repr=False, compare=False)
    _original_message: "PCIe_message | None" = field(default=None, repr=False, compare=False)
    _wire_bytes_override: int | None = field(default=None, repr=False, compare=False)

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
    _NVME_COMMAND_TYPES = {
        MessageType.WRITE_REQ,
        MessageType.READ_REQ,
        MessageType.SEARCH_REQ,
        MessageType.COMPUTE_REQ,
        MessageType.STATIC_WRITE_REQ,
    }
    _COMMAND_PHASE_DOORBELL = "doorbell"
    _COMMAND_PHASE_SQ_READ_REQUEST = "sq_read_request"
    _COMMAND_PHASE_SQ_ENTRY = "sq_entry"

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
        if target == self.host and self._is_internal_queue_release(message):
            # NVMe does not send a device-to-host PCIe acknowledgement when
            # the controller fetches an SQ entry.  These *_RECEIVED messages
            # are simulator-internal flow-control notifications, so deliver
            # them as zero-delay host events without entering the D2H FIFO.
            self.Register_sim_event(
                EventType.DELIVER,
                self.host,
                {"message": message},
                self.engine.current_time,
            )
            return

        if target == self.device and self._is_nvme_command(message):
            # NVMe command submission is a causal three-transfer exchange:
            #   H2D doorbell -> D2H SQ memory-read request -> H2D SQE data.
            # Keeping the phases on their real directions is essential under
            # contention because the two PCIe directions have independent
            # FIFOs and can operate concurrently.
            self._enqueue_transfer(
                self._make_command_phase(
                    message,
                    self._COMMAND_PHASE_DOORBELL,
                    PCIE_TLP_PACKET_OVERHEAD_BYTES + 2,
                ),
                self.device,
            )
            return

        self._enqueue_transfer(message, target)

    def _enqueue_transfer(self, message, target):
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
                self._start_head_transfer(message, self.device.hil)
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
                self._start_head_transfer(message, self.host)

    def _start_head_transfer(self, message, delivery_target):
        """Start service for the head of one directional PCIe FIFO."""
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_pcie_transfer_started(message, self.engine.current_time)
        estimated_finish_time = self.engine.current_time + self.estimate_latency(message)
        self.Register_sim_event(
            EventType.DELIVER,
            self,
            {"target": delivery_target},
            estimated_finish_time,
        )

    def _make_command_phase(self, original_message, phase, wire_bytes):
        return PCIe_message(
            type=original_message.type,
            payload=original_message.payload,
            _nvme_command_phase=phase,
            _original_message=original_message,
            _wire_bytes_override=wire_bytes,
        )

    def _advance_command_phase(self, message, event):
        phase = message._nvme_command_phase
        original_message = message._original_message
        assert original_message is not None
        if phase == self._COMMAND_PHASE_DOORBELL:
            self._enqueue_transfer(
                self._make_command_phase(
                    original_message,
                    self._COMMAND_PHASE_SQ_READ_REQUEST,
                    PCIE_TLP_PACKET_OVERHEAD_BYTES + 4,
                ),
                self.host,
            )
            return
        if phase == self._COMMAND_PHASE_SQ_READ_REQUEST:
            self._enqueue_transfer(
                self._make_command_phase(
                    original_message,
                    self._COMMAND_PHASE_SQ_ENTRY,
                    self._tlp_wire_bytes(PCIE_NVME_SQ_ENTRY_BYTES),
                ),
                self.device,
            )
            return
        if phase == self._COMMAND_PHASE_SQ_ENTRY:
            event.param["message"] = original_message
            self.device.hil.execute(event)
            return
        raise ValueError(f"Unexpected NVMe command phase: {phase}")

    def estimate_latency(self, message):
        transfer_bytes = self._estimate_transfer_bytes(message)
        bandwidth = PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS
        if bandwidth <= 0:
            raise ValueError("PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS must be positive")
        return ceil(transfer_bytes / bandwidth)

    def _estimate_transfer_bytes(self, message) -> int:
        """Return wire bytes using MQSim's PCIe/NVMe transfer model.

        Public command messages retain the aggregate isolated-command cost for
        callers of estimate_latency().  send() decomposes them into doorbell,
        SQ read-request, and SQ-entry phases whose private byte overrides are
        handled first here.  Payload and CQ transfers use the same 128-B TLP
        and 28-B per-packet overhead as MQSim.  *_RECEIVED messages are
        internal queue-release notifications and consume no link time.
        """
        override = getattr(message, "_wire_bytes_override", None)
        if override is not None:
            return override
        message_type = getattr(message, "type", None)
        if message_type in self._NVME_COMMAND_TYPES:
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
            MessageType.SEARCH_DATA_RECEIVED,
            MessageType.COMPUTE_DATA_RECEIVED,
            MessageType.STATIC_WRITE_DATA_RECEIVED,
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
    def _is_internal_queue_release(message) -> bool:
        return getattr(message, "type", None) in (
            MessageType.WRITE_DATA_RECEIVED,
            MessageType.READ_REQ_RECEIVED,
            MessageType.SEARCH_DATA_RECEIVED,
            MessageType.COMPUTE_DATA_RECEIVED,
            MessageType.STATIC_WRITE_DATA_RECEIVED,
        )

    def _is_nvme_command(self, message) -> bool:
        return (
            getattr(message, "type", None) in self._NVME_COMMAND_TYPES
            and getattr(message, "_nvme_command_phase", None) is None
        )

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
        if message._nvme_command_phase is not None:
            self._advance_command_phase(message, event)
        else:
            target.execute(event)
        if target == self.device.hil:
            if len(self.host_to_device_queue) > 0:
                new_message = self.host_to_device_queue[0]
                self._start_head_transfer(new_message, self.device.hil)
        elif target == self.host:
            if len(self.device_to_host_queue) > 0:
                new_message = self.device_to_host_queue[0]
                self._start_head_transfer(new_message, self.host)
