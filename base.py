from .memory import AnalyticalMemoryBackend
from ..types import DeviceType, LayerType


class BaseDevice:
    yaml_key: str | None = None

    @property
    def parallelism(self) -> int:
        return 1

    def __init__(self, device_type: DeviceType, config, scaling_factor):
        self.device_type = device_type
        self.config = config
        self.name = device_type
        self.peak_flops = 0
        self.peak_memory_bandwidth = 0
        self.max_interface_bandwidth = config.get("INTERFACE_BW", 0)
        self.energy_table = config.get("ENERGY_TABLE", {})
        self.power_idle_w = float(config.get("POWER_IDLE_W", 0.0) or 0.0)
        self.power_active_w = float(config.get("POWER_ACTIVE_W", 0.0) or 0.0)
        self.max_compute_util = scaling_factor.get("MAX_COMPUTE_UTIL", 1.0)
        self.max_memory_util = scaling_factor.get("MAX_OFF_MEM_BW_UTIL", 1.0)
        self.total_capacity = config.get("MEM_CAPACITY_PER_DEVICE", config.get("MEM_CAPACITY", 0))
        self.kernel_launch_overhead = 0
        self.memory_backend = AnalyticalMemoryBackend()

    def reset_memory(self):
        return

    def get_compute_latency_energy(self, layer):
        raise NotImplementedError()

    def get_interface_latency_energy(self, traffic_bytes):
        if self.max_interface_bandwidth <= 0:
            return 0, 0
        latency = traffic_bytes / (self.max_interface_bandwidth / 2)
        energy = traffic_bytes * self.energy_table.get("comm", 0)
        return latency, energy

    def get_time_and_energy(self, layer):
        if layer.type in [LayerType.X2G, LayerType.G2G]:
            return self.get_interface_latency_energy(layer.get_comm_bytes())
        return self.get_compute_latency_energy(layer)

    def _get_memory_latency(self, layer, traffic_bytes, *, scope="offchip"):
        latency, _meta = self.memory_backend.get_latency(self, layer, traffic_bytes, scope=scope)
        return latency
