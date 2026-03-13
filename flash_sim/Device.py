# -*- coding: utf-8 -*-
"""SSD 设备顶层模块，包含 HIL。"""

from .HIL import HIL


class Device:
    def __init__(self, host):
        self._construction_valid: bool = False
        self.host = host
        self.hil = HIL(name="HIL", host=host, device=self)

    def execute(self, event):
        # 目标为 device 的事件委托给 HIL 处理（若事件 target 为 device 则 param 通常给 HIL）
        self.hil.execute(event)

    def Start_simulation(self):
        pass

    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating Device construction...")
        assert self.host is not None, "Device host is not set"
        assert self.hil is not None, "Device hil is not set"
        self.hil.Validate_construction()
        self._construction_valid = True
        print("Device construction validation complete.")