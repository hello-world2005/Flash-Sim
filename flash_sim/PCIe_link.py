# -*- coding: utf-8 -*-
from collections import deque
from typing import TYPE_CHECKING, Any

from .common import EventType, Register_event
from dataclasses import dataclass
from .common import MessageType, Request

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
        if target == self.device:
            self.host_to_device_queue.append(message)
            if len(self.host_to_device_queue) == 1:
                estimated_latency = self.estimate_latency(message)
                estimated_finish_time = self.engine.current_time + estimated_latency
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.device.hil}, estimated_finish_time)
        elif target == self.host:
            self.device_to_host_queue.append(message)
            if len(self.device_to_host_queue) == 1:
                estimated_latency = self.estimate_latency(message)
                estimated_finish_time = self.engine.current_time + estimated_latency
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.host}, estimated_finish_time)

    def estimate_latency(self, message):
        return 100

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
        target.execute(event)
        if target == self.device.hil:
            if len(self.host_to_device_queue) > 0:
                new_message = self.host_to_device_queue[0]
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.device.hil}, self.engine.current_time + self.estimate_latency(new_message))
        elif target == self.host:
            if len(self.device_to_host_queue) > 0:
                new_message = self.device_to_host_queue[0]
                self.Register_sim_event(EventType.DELIVER, self, {"target": self.host}, self.engine.current_time + self.estimate_latency(new_message))
