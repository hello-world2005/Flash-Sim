# -*- coding: utf-8 -*-
from queue import PriorityQueue

import Host
import PCIe_link
import Device
from common import SimEvent


class Engine:
    def __init__(self):
        self.current_time = 0
        self.host = Host.Host("Host", num_of_queues=8, depth_of_queues=64)
        self.device = Device.Device(self.host)
        self.pcie_link = PCIe_link.PCIe_link(self.host, self.device)
        self.host.pcie_link = self.pcie_link
        self.pcie_link.engine = self
        self.event_queue = PriorityQueue()

    def Register_event(self, event_type, target, param, scheduled_time):
        self.event_queue.put((scheduled_time, event_type, target, param))

    def Execute_event(self):
        scheduled_time, event_type, target, param = self.event_queue.get()
        self.current_time = scheduled_time
        event = SimEvent(type=event_type, target=target, param=param)
        target.execute(event)

    def Run(self):
        while not self.event_queue.empty():
            self.Execute_event()
