# -*- coding: utf-8 -*-
from pathlib import Path
from queue import PriorityQueue

if __package__ in (None, ""):
    import os
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from flash_sim import Host
    from flash_sim import PCIe_link
    from flash_sim import Device
    from flash_sim import common as _common
    from flash_sim.common import EventType, SimEvent, Request, RequestType, format_event_queue
    from flash_sim.parser import parse_trace
    from flash_sim.request_latency_report import RequestLatencyRecorder
else:
    from . import Host
    from . import PCIe_link
    from . import Device
    from . import common as _common
    from .common import EventType, SimEvent, Request, RequestType, format_event_queue
    from .parser import parse_trace
    from .request_latency_report import RequestLatencyRecorder

class Engine:
    def __init__(self):
        print("Initializing simulation engine...")
        self._construction_valid: bool = False
        self.current_time = 0
        self.event_queue = PriorityQueue()
        self.repo_root = Path(__file__).resolve().parents[1]

        # Provide simulation time and event scheduling to all modules via common.py
        _common._time_provider = lambda: self.current_time
        _common._event_scheduler = self.Register_event
        self.request_latency_recorder = RequestLatencyRecorder()
        self.request_latency_recorder.attach(self)
        _common.SET_REQUEST_LATENCY_RECORDER(self.request_latency_recorder)
        self.last_request_latency_report_path: Path | None = None
        self.last_request_latency_csv_path: Path | None = None

        self.host = Host.Host("Host", num_of_queues=8, depth_of_queues=64)
        self.device = Device.Device(self.host)
        self.pcie_link = PCIe_link.PCIe_link(self.host, self.device)
        self.host.pcie_link = self.pcie_link
        self.pcie_link.engine = self
        print("Engine initialization complete.")

    def Register_event(self, event_type, target, param, scheduled_time):
        event = SimEvent(type=event_type, target=target, time=scheduled_time, param=param)
        self.event_queue.put(event)
        print(f"[Engine] Time = {self.current_time}, Register_event: type={event_type}, scheduled_time={scheduled_time}, target={target.__class__.__name__}, param={param}")
        print()
        return event

    def Execute_event(self):
        event = self.event_queue.get()
        if event.ignored:
            return
        self.current_time = event.time
        event.target.execute(event)

    def Run(self):
        while not self.event_queue.empty():
            self.Execute_event()
    
    def Get_current_time(self):
        return self.current_time
    
    def Initialize_event_queue(self, trace_path: str):
        """从 trace 文件解析请求，通过 Register_event 将每个 req 作为 event.param 压入 event_queue。"""
        commands = parse_trace(trace_path, mode="engine")
        self.request_latency_recorder.set_trace_context(trace_path)
        for trace_index, cmd in enumerate(commands):
            scheduled_time = cmd["time"]
            req = Request(
                type=RequestType(cmd["type"].upper()),
                stream_id=cmd.get("stream_id", 0),
                sq_id=None,
                transaction_list=[],
                lha_start=cmd["start_lha"],
                size=cmd["size"],
                trace_index=trace_index,
                trace_time=scheduled_time,
                report_req_id=f"req-{trace_index:04d}-{cmd['type']}-{cmd['start_lha']}-{cmd['size']}",
            )
            if cmd.get("invalidate") == 1: # for invalidative write
                req.invalidate = True
            self.request_latency_recorder.register_request(req, scheduled_time)
            self.Register_event(EventType.REQ_INIT, self.host, {"req": req}, scheduled_time)
    
    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.event_queue is not None, "Engine event_queue is not set"
        assert self.host is not None, "Engine host is not set"
        assert self.device is not None, "Engine device is not set"
        assert self.pcie_link is not None, "Engine pcie_link is not set"
        self.host.Validate_construction()
        self.device.Validate_construction()
        self.pcie_link.Validate_construction()
        self._construction_valid = True
        print("Construction validation complete.") 

    def Start_simulation(self, trace_path, pre_trace=None):
        self.Validate_construction()
        # 在 validation 之后执行 preconditioning 阶段
        self.device.ftl.block_manager.preconditioning(
            data_path=pre_trace,
            phy=self.device.ftl.tsu.phy,
            amu=self.device.ftl.address_mapping_unit,
        )
        self.Initialize_event_queue(trace_path)
        print("Event queue initialization complete.\n\n")
        print(format_event_queue(self.event_queue.queue))
        print("--------------------------------------------------------\n")
        print("Starting simulation...\n")
        print("--------------------------------------------------------\n")
        self.Run()
        self._finalize_pending_cache_flushes()
        self._export_request_latency_report()

    def _finalize_pending_cache_flushes(self):
        cache_manager = self.device.hil.cache_manager
        while cache_manager.has_pending_entries():
            if not cache_manager.has_flushable_entries():
                break
            flushed = cache_manager.write_flush()
            if not flushed:
                break
            self.Run()

    def _export_request_latency_report(self):
        report_dir = self.repo_root / "report"
        report_path = self.request_latency_recorder.derive_report_path(report_dir)
        csv_path = self.request_latency_recorder.derive_csv_report_path(report_dir)
        self.last_request_latency_report_path = self.request_latency_recorder.dump_json(report_path)
        self.last_request_latency_csv_path = self.request_latency_recorder.dump_csv(csv_path)

