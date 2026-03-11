# -*- coding: utf-8 -*-
"""SSD 设备顶层模块，包含 HIL。"""

from common import sim_object
from HIL import HIL


class Device(sim_object):
    def __init__(self, host):
        self.host = host
        self.hil = HIL(name="HIL", host=host, device=self)

    def execute(self, event):
        # 目标为 device 的事件委托给 HIL 处理（若事件 target 为 device 则 param 通常给 HIL）
        self.hil.execute(event)

    def Start_simulation(self):
        self.hil.Setup_triggers()
