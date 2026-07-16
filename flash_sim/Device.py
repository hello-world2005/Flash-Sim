# -*- coding: utf-8 -*-
"""SSD 设备顶层模块，包含 HIL。"""

from .HIL import HIL
from .FTL import FTL
from .PHY import PHY
from .common import COMPUTE_MAX_PARALLEL_SL, QUIET, WL_PER_STRING

class Device:
    def __init__(
        self,
        host,
        cache_bypass: bool = False,
        data_cache_capacity: int | None = None,
        onfi_timing=None,
        cim_geometry=None,
    ):
        self._construction_valid: bool = False
        self.host = host
        self.hil = HIL(
            name="HIL",
            host=host,
            device=self,
            cache_bypass=cache_bypass,
            data_cache_capacity=data_cache_capacity,
            wl_per_string=(
                cim_geometry.wl_per_string if cim_geometry is not None else WL_PER_STRING
            ),
        )
        self.ftl = FTL(
            compute_max_parallel_sl=(
                cim_geometry.compute_max_parallel_sl
                if cim_geometry is not None
                else COMPUTE_MAX_PARALLEL_SL
            )
        )
        self.phy = PHY(onfi_timing=onfi_timing, cim_geometry=cim_geometry)
        self.hil.ftl = self.ftl
        self.ftl.block_manager.cache_manager = self.hil.cache_manager
        self.ftl.tsu.phy = self.phy
        self.phy.connect_channel_idle_signal(self.ftl.tsu._on_channel_idle)
        self.phy.connect_chip_idle_signal(self.ftl.tsu._on_chip_idle)
        self.phy.connect_transaction_serviced_signal(self.hil._on_transaction_serviced)
        self.phy.connect_transaction_serviced_signal(self.ftl.address_mapping_unit._handle_mapping_response)
        self.phy.connect_transaction_serviced_signal(self.ftl.block_manager._on_transaction_serviced)
        self.phy.connect_transaction_serviced_signal(self.ftl.tsu._reschedule)

    def execute(self, event):
        # from .common import log_execute_event
        # log_execute_event(self.__class__.__name__, event)
        # 目标为 device 的事件委托给 HIL 处理（若事件 target 为 device 则 param 通常给 HIL）
        self.hil.execute(event)

    def Start_simulation(self):
        pass

    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating Device construction...")
        assert self.host is not None, "Device host is not set"
        assert self.hil is not None, "Device hil is not set"
        assert self.ftl is not None, "Device ftl is not set"
        assert self.phy is not None, "Device phy is not set"
        self._construction_valid = True
        self.ftl.Validate_construction()
        self.phy.Validate_construction()
        self.hil.Validate_construction()
        if not QUIET:
            print("Device construction validation complete.")
