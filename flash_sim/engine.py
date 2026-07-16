# -*- coding: utf-8 -*-
import heapq
import os
from pathlib import Path
from collections import defaultdict
from typing import Any

if __package__ in (None, ""):
    import sys

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from flash_sim import Host
    from flash_sim import PCIe_link
    from flash_sim import Device
    from flash_sim import common as _common
    from flash_sim.common import EventType, SimEvent, Request, RequestType, format_event_queue
    from flash_sim.config import FlashConfig
    from flash_sim.parser import parse_trace
    from flash_sim.request_latency_report import RequestLatencyRecorder
else:
    from . import Host
    from . import PCIe_link
    from . import Device
    from . import common as _common
    from .common import EventType, SimEvent, Request, RequestType, format_event_queue
    from .config import FlashConfig
    from .parser import parse_trace
    from .request_latency_report import RequestLatencyRecorder


def _event_progress_interval_from_env() -> int:
    raw = os.environ.get("FLASHSIM_EVENT_PROGRESS_INTERVAL", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


class _EventHeapQueue:
    def __init__(self) -> None:
        self.queue: list[SimEvent] = []

    def put(self, event: SimEvent) -> None:
        heapq.heappush(self.queue, event)

    def get(self) -> SimEvent:
        return heapq.heappop(self.queue)

    def empty(self) -> bool:
        return not self.queue

    def qsize(self) -> int:
        return len(self.queue)


READ_LIKE_PRECONDITION_TYPES = {"read", "search", "compute"}


def _command_lpa_range(cmd: dict[str, Any], sector_per_page: int) -> range:
    size = int(cmd.get("size", 0) or 0)
    if size <= 0:
        return range(0)
    start_lha = int(cmd.get("start_lha", cmd.get("start_lba", 0)) or 0)
    end_lha = start_lha + size - 1
    return range(start_lha // sector_per_page, end_lha // sector_per_page + 1)


def _metadata_blocks_by_plane(amu) -> dict[tuple[int, int, int, int], set[int]]:
    metadata_blocks: dict[tuple[int, int, int, int], set[int]] = defaultdict(set)
    for mvpn in range(amu.mapping_page_count):
        addr = amu.get_plane_address_for_mvpn(mvpn)
        metadata_blocks[(addr.channel, addr.chip, addr.die, addr.plane)].add(addr.sub_plane)
    return metadata_blocks


def _preconditionable_plane_capacities(block_manager, amu) -> dict[tuple[int, int, int, int], int]:
    metadata_blocks = _metadata_blocks_by_plane(amu)
    capacities: dict[tuple[int, int, int, int], int] = {}
    for channel_id in range(block_manager.channel_no):
        for chip_id in range(block_manager.chip_no_per_channel):
            if block_manager._is_static_chip(chip_id):
                continue
            for die_id in range(block_manager.die_no_per_chip):
                for plane_id in range(block_manager.plane_no_per_die):
                    key = (channel_id, chip_id, die_id, plane_id)
                    metadata_count = len(metadata_blocks.get(key, set()))
                    allocatable_blocks = max(0, block_manager.block_no_per_plane - metadata_count)
                    writable_blocks = max(
                        0,
                        allocatable_blocks
                        - _common.GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD
                        - 1,
                    )
                    capacities[key] = writable_blocks * block_manager.pages_per_block
    return capacities


def _generate_precondition_from_trace(
    trace_path: str,
    fill_ratio: float,
    block_manager,
    amu,
    *,
    mode: str = "capacity-fill",
    seed: int = 42,
) -> dict[str, Any]:
    import random

    mode = mode.lower()
    if mode not in ("capacity-fill", "trace-cover"):
        raise ValueError(f"unsupported precondition mode: {mode}")
    commands = parse_trace(trace_path, mode="engine")
    sector_per_page = _common.SECTOR_PER_PAGE
    capacities = _preconditionable_plane_capacities(block_manager, amu)
    preconditionable_pages = sum(capacities.values())
    requested_target_pages = int(preconditionable_pages * fill_ratio)

    def plane_key_for_lpa(lpa: int) -> tuple[int, int, int, int] | None:
        try:
            addr = amu.get_plane_address_for_lpa(lpa)
        except Exception:
            return None
        key = (addr.channel, addr.chip, addr.die, addr.plane)
        if key not in capacities:
            return None
        return key

    trace_read_lpas: set[int] = set()
    mandatory_read_lpas: set[int] = set()
    trace_write_lpas: set[int] = set()
    seen_written_lpas: set[int] = set()
    for cmd in commands:
        command_type = str(cmd.get("type", "")).lower()
        for lpa in _command_lpa_range(cmd, sector_per_page):
            key = plane_key_for_lpa(lpa)
            if key is None:
                continue
            if command_type in READ_LIKE_PRECONDITION_TYPES:
                trace_read_lpas.add(lpa)
                if lpa not in seen_written_lpas:
                    mandatory_read_lpas.add(lpa)
            else:
                trace_write_lpas.add(lpa)
                seen_written_lpas.add(lpa)

    selected_by_plane: dict[tuple[int, int, int, int], set[int]] = {
        key: set() for key in capacities
    }
    selected_sources: dict[int, str] = {}
    read_seed = trace_read_lpas if mode == "trace-cover" else mandatory_read_lpas
    for lpa in sorted(read_seed):
        key = plane_key_for_lpa(lpa)
        if key is None:
            continue
        if len(selected_by_plane[key]) >= capacities[key]:
            raise ValueError(
                "precondition read coverage exceeds preconditionable capacity "
                f"for plane {key}"
            )
        selected_by_plane[key].add(lpa)
        selected_sources[lpa] = "trace_read"

    per_plane_targets = {
        key: min(capacity, max(int(capacity * fill_ratio), len(selected_by_plane[key])))
        for key, capacity in capacities.items()
    }

    rng = random.Random(seed)
    trace_writes_by_plane: dict[tuple[int, int, int, int], list[int]] = defaultdict(list)
    for lpa in sorted(trace_write_lpas):
        if lpa in selected_sources:
            continue
        key = plane_key_for_lpa(lpa)
        if key is not None:
            trace_writes_by_plane[key].append(lpa)
    for key in sorted(trace_writes_by_plane):
        candidates = trace_writes_by_plane[key]
        rng.shuffle(candidates)
        for lpa in candidates:
            if len(selected_by_plane[key]) >= per_plane_targets[key]:
                break
            selected_by_plane[key].add(lpa)
            selected_sources[lpa] = "trace_write"

    if mode == "capacity-fill":
        remaining_planes = {
            key
            for key in capacities
            if len(selected_by_plane[key]) < per_plane_targets[key]
        }
        data_pages = int(getattr(amu, "random_access_data_pages", 0))
        start_lpa = rng.randrange(data_pages) if data_pages > 0 else 0
        for offset in range(data_pages):
            if not remaining_planes:
                break
            lpa = (start_lpa + offset) % data_pages
            if lpa in selected_sources:
                continue
            key = plane_key_for_lpa(lpa)
            if key not in remaining_planes:
                continue
            selected_by_plane[key].add(lpa)
            selected_sources[lpa] = "filler"
            if len(selected_by_plane[key]) >= per_plane_targets[key]:
                remaining_planes.discard(key)

    selected_lpas = sorted(selected_sources)
    plane_actual_counts = [len(lpas) for lpas in selected_by_plane.values()]
    plane_target_counts = list(per_plane_targets.values())
    stats = {
        "mode": mode,
        "requested_fill_ratio": fill_ratio,
        "seed": seed,
        "plane_allocation": getattr(amu, "plane_allocation_scheme", getattr(amu, "_plane_allocation_scheme", "PAGE_LEVEL")),
        "preconditionable_pages": preconditionable_pages,
        "requested_target_pages": requested_target_pages,
        "planned_pages": len(selected_lpas),
        "planned_fill_ratio": (len(selected_lpas) / preconditionable_pages) if preconditionable_pages else 0.0,
        "trace_read_pages": len(trace_read_lpas),
        "mandatory_read_pages": len(mandatory_read_lpas),
        "trace_write_pages": len(trace_write_lpas),
        "selected_trace_read_pages": sum(1 for source in selected_sources.values() if source == "trace_read"),
        "selected_trace_write_pages": sum(1 for source in selected_sources.values() if source == "trace_write"),
        "filler_pages": sum(1 for source in selected_sources.values() if source == "filler"),
        "plane_count": len(capacities),
        "plane_target_min": min(plane_target_counts) if plane_target_counts else 0,
        "plane_target_max": max(plane_target_counts) if plane_target_counts else 0,
        "plane_actual_min": min(plane_actual_counts) if plane_actual_counts else 0,
        "plane_actual_max": max(plane_actual_counts) if plane_actual_counts else 0,
        "underfilled_planes": sum(
            1
            for key, target in per_plane_targets.items()
            if len(selected_by_plane[key]) < target
        ),
    }
    if not _common.QUIET:
        print(
            "[Engine] Auto-generated precondition: "
            f"{len(selected_lpas)} pages "
            f"(mode={mode}, fill_ratio={fill_ratio}, "
            f"target={requested_target_pages}, filler={stats['filler_pages']})",
            flush=True,
        )
    return {"lpas": selected_lpas, "stats": stats, "data_value": 0xAA}


class Engine:
    def __init__(self, config: FlashConfig | None = None):
        if not _common.QUIET:
            print("Initializing simulation engine...")
        self._construction_valid: bool = False
        self.current_time = 0
        self.executed_event_count = 0
        self._event_progress_interval = _event_progress_interval_from_env()
        self.event_queue = _EventHeapQueue()
        self.repo_root = Path(__file__).resolve().parents[1]

        _common._time_provider = lambda: self.current_time
        _common._event_scheduler = self.Register_event
        self.request_latency_recorder = RequestLatencyRecorder()
        self.request_latency_recorder.attach(self)
        _common.SET_REQUEST_LATENCY_RECORDER(self.request_latency_recorder)
        self.last_request_latency_report_path: Path | None = None
        self.last_request_latency_csv_path: Path | None = None

        self.config = config or FlashConfig()
        self.host = Host.Host("Host", num_of_queues=8, depth_of_queues=64)
        self.device = Device.Device(
            self.host,
            cache_bypass=self.config.runtime.cache_bypass,
            data_cache_capacity=self.config.runtime.data_cache_capacity,
            onfi_timing=self.config.onfi,
            cim_geometry=self.config.geometry,
        )
        self.device.ftl.apply_runtime_config(self.config.runtime)
        self.pcie_link = PCIe_link.PCIe_link(self.host, self.device)
        self.host.pcie_link = self.pcie_link
        self.pcie_link.engine = self
        if not _common.QUIET:
            print("Engine initialization complete.")

    def Register_event(self, event_type, target, param, scheduled_time):
        event = SimEvent(type=event_type, target=target, time=scheduled_time, param=param)
        self.event_queue.put(event)
        return event

    def Execute_event(self):
        event = self.event_queue.get()
        if not event.ignored:
            self.current_time = event.time
        self.executed_event_count += 1
        self._maybe_print_event_progress(event)
        if event.ignored:
            return
        event.target.execute(event)

    def _event_progress_detail(self, event: SimEvent) -> str:
        if event.type == EventType.REQ_INIT:
            req = event.param.get("req")
            req_id = getattr(req, "report_req_id", None)
            return f" req_id={req_id}" if req_id is not None else ""
        if event.type == EventType.DELIVER:
            message = event.param.get("message")
            message_type = getattr(getattr(message, "type", None), "value", None)
            return f" message={message_type}" if message_type is not None else ""
        return ""

    def _maybe_print_event_progress(self, event: SimEvent) -> None:
        interval = self._event_progress_interval
        if interval <= 0 or self.executed_event_count % interval != 0:
            return

        target_name = getattr(event.target, "__class__", type(event.target)).__name__
        completed = getattr(self.host, "completed_request_count", "n/a")
        errors = getattr(self.host, "completed_error_count", "n/a")
        waiting = self.host.waiting_req.qsize() if hasattr(self.host, "waiting_req") else "n/a"
        sq_queued = (
            sum(len(entries) for entries in self.host.memory.sq_entries)
            if hasattr(self.host, "memory")
            else "n/a"
        )
        event_type = getattr(event.type, "value", str(event.type))
        maintenance = self.request_latency_recorder.maintenance_stats
        gc_queue_total = 0
        try:
            tsu = self.device.ftl.tsu
            gc_types = (
                _common.TransactionType.GC_READ,
                _common.TransactionType.GC_WRITE,
                _common.TransactionType.GC_ERASE,
            )
            for chips in tsu.queues:
                for queues in chips:
                    gc_queue_total += sum(len(queues.get(tr_type, [])) for tr_type in gc_types)
        except Exception:
            gc_queue_total = -1
        print(
            "[EngineProgress] "
            f"events={self.executed_event_count} event_type={event_type} "
            f"target={target_name} event_time_ns={event.time} "
            f"current_time_ns={self.current_time} queue_size={self.event_queue.qsize()} "
            f"completed={completed} errors={errors} "
            f"waiting={waiting} sq_queued={sq_queued} "
            f"gc_count={maintenance.get('gc_count', 0)} "
            f"static_wl_count={maintenance.get('static_wl_count', 0)} "
            f"erase_count={maintenance.get('gc_erased_blocks', 0)} "
            f"tsu_gc_queue={gc_queue_total}"
            f"{self._event_progress_detail(event)}",
            flush=True,
        )

    def Run(self):
        self._drain_event_system()

    def _drain_event_system(self):
        for _ in range(100000):
            while not self.event_queue.empty():
                self.Execute_event()

            progressed = self._drain_tsu_once()
            progressed = self.device.phy.kick_all_channel_transfers() or progressed
            if self.event_queue.empty() and not progressed:
                progressed = self._trigger_waiting_write_gc()
                if progressed:
                    progressed = self._drain_tsu_once() or progressed
                    progressed = self.device.phy.kick_all_channel_transfers() or progressed
            if self.event_queue.empty() and not progressed:
                return
        raise RuntimeError("Event system did not quiesce after 100000 drain rounds")

    def _trigger_waiting_write_gc(self) -> bool:
        block_manager = self.device.ftl.block_manager
        pending_planes = [
            plane_key
            for plane_key, waiting in block_manager.waiting_writes.items()
            if waiting
        ]
        if not pending_planes:
            return False
        tsu = self.device.ftl.tsu
        before_events = self.event_queue.qsize()
        before_tsu = sum(
            len(queue)
            for chips in tsu.queues
            for queues in chips
            for queue in queues.values()
        )
        gc = self.device.ftl.gc_wl_unit
        for channel, chip, die, plane in pending_planes:
            plane_addr = _common.FlashAddress(
                channel=channel,
                chip=chip,
                die=die,
                plane=plane,
                sub_plane=-1,
                page=-1,
            )
            gc.check_gc_for_plane(plane_addr)
            block_manager._retry_waiting_writes(plane_addr)
        after_tsu = sum(
            len(queue)
            for chips in tsu.queues
            for queues in chips
            for queue in queues.values()
        )
        return self.event_queue.qsize() != before_events or after_tsu != before_tsu

    def _drain_tsu_once(self) -> bool:
        tsu = self.device.ftl.tsu
        before_events = self.event_queue.qsize()
        tsu._onfly_schedule_req_no = 0
        tsu.Schedule()
        return self.event_queue.qsize() != before_events

    def Get_current_time(self):
        return self.current_time

    def Initialize_event_queue(self, trace_path: str):
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
                selected_wl=cmd.get("selected_wl"),
                report_req_id=f"req-{trace_index:04d}-{cmd['type']}-{cmd['start_lha']}-{cmd['size']}",
            )
            if cmd.get("invalidate") == 1:
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
        if not _common.QUIET:
            print("Construction validation complete.")

    def Start_simulation(self, trace_path, pre_trace=None):
        self.Validate_construction()
        # Auto-generate precondition from trace if not provided and fill_ratio is set
        if pre_trace is None and self.config.runtime.precondition_fill_ratio is not None:
            pre_trace = _generate_precondition_from_trace(
                trace_path,
                self.config.runtime.precondition_fill_ratio,
                self.device.ftl.block_manager,
                self.device.ftl.address_mapping_unit,
                mode=self.config.runtime.precondition_mode,
                seed=self.config.runtime.precondition_seed,
            )
        self.device.ftl.block_manager.preconditioning(
            data_path=pre_trace,
            phy=self.device.ftl.tsu.phy,
            amu=self.device.ftl.address_mapping_unit,
        )
        self.Initialize_event_queue(trace_path)
        if not _common.QUIET:
            print("Event queue initialization complete.\n\n")
            _common.debug_info(format_event_queue(self.event_queue.queue))
            print("--------------------------------------------------------\n")
            print("Starting simulation...\n")
            print("--------------------------------------------------------\n")
        self.Run()
        self._finalize_pending_cache_flushes()
        self._dump_pending_state_if_requested()
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

    def _dump_pending_state_if_requested(self) -> None:
        if os.environ.get("FLASHSIM_DUMP_PENDING", "0") not in ("1", "true", "TRUE", "yes"):
            return

        print("[PendingDump] begin", flush=True)
        incomplete = [
            rec
            for rec in self.request_latency_recorder.requests.values()
            if rec.status is None
        ]
        incomplete.sort(
            key=lambda rec: (
                rec.trace_index if rec.trace_index is not None else 10**9,
                rec.req_id,
            )
        )
        print(f"[PendingDump] incomplete_requests={len(incomplete)}", flush=True)
        for rec in incomplete[:20]:
            print(
                "[PendingDump] incomplete "
                f"req_id={rec.req_id} trace_index={rec.trace_index} "
                f"type={rec.req_type} lha_start={rec.lha_start} size={rec.size} "
                f"host_completion_time={rec.host_completion_time}",
                flush=True,
            )

        amu = self.device.ftl.address_mapping_unit
        waiting_items = [
            (lpa, trs)
            for lpa, trs in amu.waiting_for_mapping_trans.items()
            if trs
        ]
        print(f"[PendingDump] amu_waiting_lpas={len(waiting_items)}", flush=True)
        for lpa, trs in waiting_items[:20]:
            req_ids = [
                tr.source_req.report_req_id
                for tr in trs[:8]
                if tr.source_req is not None
            ]
            print(
                "[PendingDump] amu_wait "
                f"lpa={lpa} count={len(trs)} req_ids={req_ids}",
                flush=True,
            )

        block_manager = self.device.ftl.block_manager
        waiting_write_items = [
            (plane_key, trs)
            for plane_key, trs in block_manager.waiting_writes.items()
            if trs
        ]
        print(f"[PendingDump] block_waiting_planes={len(waiting_write_items)}", flush=True)
        for plane_key, trs in waiting_write_items[:20]:
            sample_tr = trs[0]
            plane_addr = sample_tr.address
            plane_bke = block_manager.get_plane_bke(plane_addr)
            victim = self.device.ftl.gc_wl_unit._pick_gc_victim_block(plane_addr)
            can_gc = self.device.ftl.gc_wl_unit.can_trigger_gc(plane_addr)
            samples = []
            for tr in trs[:8]:
                req_id = tr.source_req.report_req_id if tr.source_req is not None else None
                samples.append(
                    {
                        "req_id": req_id,
                        "lpa": tr.lpa,
                        "addr": (
                            tr.address.channel,
                            tr.address.chip,
                            tr.address.die,
                            tr.address.plane,
                        ),
                    }
                )
            print(
                "[PendingDump] block_wait "
                f"plane_key={plane_key} count={len(trs)} "
                f"free_blocks={len(plane_bke.free_block_pool)} "
                f"free_pages={plane_bke.free_page_count} "
                f"valid_pages={plane_bke.valid_page_count} "
                f"invalid_pages={plane_bke.invalid_page_count} "
                f"gc_barriers={sorted(plane_bke.gc_wl_barrier_blocks)} "
                f"victim={victim} can_gc={can_gc} samples={samples}",
                flush=True,
            )

        tsu = self.device.ftl.tsu
        total_tsu = 0
        for ch, chips in enumerate(tsu.queues):
            for chip, queues in enumerate(chips):
                for tr_type, queue in queues.items():
                    if not queue:
                        continue
                    total_tsu += len(queue)
                    samples = []
                    for tr in queue[:5]:
                        req_id = tr.source_req.report_req_id if tr.source_req is not None else None
                        blocker = None
                        if getattr(tr, "mvpn", -1) >= 0:
                            book_mvpn = tsu.block_manager.mvpn_protected_book.get(tr.mvpn)
                            if book_mvpn is not None and book_mvpn is not tr:
                                blocker = {
                                    "kind": getattr(book_mvpn.type, "value", str(book_mvpn.type)),
                                    "mvpn": book_mvpn.mvpn,
                                    "completed": book_mvpn.completed,
                                    "deps": len(book_mvpn.rely_on_transactions),
                                }
                        if blocker is None and getattr(tr, "lpa", -1) >= 0:
                            book_lpa = tsu.block_manager.lpa_protected_book.get(tr.lpa)
                            if book_lpa is not None and book_lpa is not tr:
                                blocker = {
                                    "kind": getattr(book_lpa.type, "value", str(book_lpa.type)),
                                    "lpa": book_lpa.lpa,
                                    "completed": book_lpa.completed,
                                    "deps": len(book_lpa.rely_on_transactions),
                                }
                        samples.append(
                            {
                                "req_id": req_id,
                                "lpa": tr.lpa,
                                "mvpn": tr.mvpn,
                                "addr": (
                                    tr.address.channel,
                                    tr.address.chip,
                                    tr.address.die,
                                    tr.address.plane,
                                    tr.address.sub_plane,
                                    tr.address.page,
                                ),
                                "deps": len(tr.rely_on_transactions),
                                "data_ready": tr.data_ready,
                                "blocked": tsu._transaction_blocked_by_barrier(tr),
                                "blocker": blocker,
                            }
                        )
                    print(
                        "[PendingDump] tsu_queue "
                        f"ch={ch} chip={chip} type={getattr(tr_type, 'value', tr_type)} "
                        f"count={len(queue)} samples={samples}",
                        flush=True,
                    )
        print(f"[PendingDump] tsu_total={total_tsu}", flush=True)

        phy = self.device.phy
        active_count = sum(1 for task in phy._active_transfers if task is not None)
        pending_count = sum(len(tasks) for tasks in phy._pending_transfers)
        print(
            f"[PendingDump] phy_active_transfers={active_count} "
            f"phy_pending_transfers={pending_count}",
            flush=True,
        )
        for ch, task in enumerate(phy._active_transfers):
            if task is not None:
                print(
                    "[PendingDump] phy_active "
                    f"ch={ch} kind={task.kind.value} chip={task.chip_id} die={task.die_id} "
                    f"finish={task.finish_time} remaining={task.remaining_duration}",
                    flush=True,
                )
        for ch, tasks in enumerate(phy._pending_transfers):
            if not tasks:
                continue
            sample = [
                {
                    "kind": task.kind.value,
                    "chip": task.chip_id,
                    "die": task.die_id,
                    "count": len(task.transactions),
                    "req_ids": [
                        tr.source_req.report_req_id
                        for tr in task.transactions[:4]
                        if tr.source_req is not None
                    ],
                }
                for task in tasks[:5]
            ]
            print(
                "[PendingDump] phy_pending "
                f"ch={ch} count={len(tasks)} sample={sample}",
                flush=True,
            )

        active_chips = []
        for chip_id, chip_bke in phy._chip_bkes.items():
            if (
                chip_bke.status != _common.ChipStatus.IDLE
                or chip_bke.No_of_active_dies != 0
                or chip_bke.HasSuspendedCommands
                or chip_bke._has_data_waiting
            ):
                active_chips.append(
                    {
                        "chip": chip_id,
                        "status": chip_bke.status.value,
                        "active_dies": chip_bke.No_of_active_dies,
                        "suspended": chip_bke.HasSuspendedCommands,
                        "data_waiting": chip_bke._has_data_waiting,
                    }
                )
        print(f"[PendingDump] non_idle_chips={active_chips[:20]}", flush=True)
        print("[PendingDump] end", flush=True)

    def _export_request_latency_report(self):
        report_dir = self.repo_root / "report"
        report_path = self.request_latency_recorder.derive_report_path(report_dir)
        csv_path = self.request_latency_recorder.derive_csv_report_path(report_dir)
        self.last_request_latency_report_path = self.request_latency_recorder.dump_json(report_path)
        self.last_request_latency_csv_path = self.request_latency_recorder.dump_csv(csv_path)
