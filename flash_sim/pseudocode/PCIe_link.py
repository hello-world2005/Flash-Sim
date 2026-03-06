# -*- coding: utf-8 -*-
from queue import Queue

from common import sim_object, DELIVER


class PCIe_message:
    def __init__(self, type, payload, source_req, sq_id=None):
        self.type = type
        self.payload = payload
        self.source_req = source_req
        self.sq_id = sq_id if sq_id is not None else (getattr(source_req, "sq_id", None))


class PCIe_link(sim_object):
    def __init__(self, host, device):
        self.host = host
        self.device = device
        self.engine = None
        self.host_to_device_queue = Queue()
        self.device_to_host_queue = Queue()

    def send(self, message, target):
        if target == self.device:
            self.host_to_device_queue.put(message)
            estimated_latency = self.estimate_latency(message)
            estimated_finish_time = self.engine.current_time + estimated_latency
            self.Register_sim_event(DELIVER, self.device, message, estimated_finish_time)
        elif target == self.host:
            self.device_to_host_queue.put(message)
            estimated_latency = self.estimate_latency(message)
            estimated_finish_time = self.engine.current_time + estimated_latency
            self.Register_sim_event(DELIVER, self.host, message, estimated_finish_time)

    def estimate_latency(self, message):
        return 100

    def Register_sim_event(self, event_type, target, param, scheduled_time):
        self.engine.Register_event(event_type, target, param, scheduled_time)

    def execute(self, event):
        assert event.type == DELIVER
        message = event.param
        target = event.target
        if target == self.device:
            new_message = self.host_to_device_queue.get() if not self.host_to_device_queue.empty() else None
            if new_message is not None:
                self.send(new_message, self.device)
        elif target == self.host:
            new_message = self.device_to_host_queue.get() if not self.device_to_host_queue.empty() else None
            if new_message is not None:
                self.send(new_message, self.host)
        target.receive_pcie_message(message)
