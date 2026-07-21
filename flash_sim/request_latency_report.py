from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any, Optional

from .common import (
    MessageType,
    PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS,
    PCIE_TLP_MAX_PAYLOAD_BYTES,
    PCIE_TLP_PACKET_OVERHEAD_BYTES,
    REQUEST_STATUS_SUCCESS,
    Request,
    RequestType,
    SECTOR_PER_PAGE,
    SECTOR_SIZE_BYTES,
    Transaction,
    TransactionType,
)


BASE_STAGE_NAMES = (
    "host_sq_wait",
    "host_dispatch",
    "pcie_host_to_device",
    "pcie_device_to_host",
    "amu_mapping_wait",
    "tsu_queue_wait",
    "phy_channel_wait",
    "phy_cmd_addr",
    "phy_data_in",
    "phy_array_exec",
    "phy_data_out",
)

# Diagnostic children of the legacy enqueue-to-delivery PCIe stages.  They
# are exported but excluded from reconciliation to avoid double counting the
# same PCIe interval together with its parent stage.
PCIE_DETAIL_STAGE_NAMES = (
    "pcie_host_to_device_queue_wait",
    "pcie_device_to_host_queue_wait",
    "pcie_host_to_device_wire",
    "pcie_device_to_host_wire",
)

ALL_INTERVAL_STAGE_NAMES = BASE_STAGE_NAMES + PCIE_DETAIL_STAGE_NAMES

RECONCILIATION_STAGE_NAMES = ("overlap_latency", "untracked_latency")

CSV_COLUMN_NAMES = (
    "Issue Time",
    "REQ Type",
    "Finish Time",
    # Host/controller-side processing and waiting
    "Time in SQ",
    "Cache Hit",
    "Mapping",
    "Time in TSU",
    "Backpressure Wait Time",
    # PCIe aggregate and raw queue/service details
    "PCIe Xfer",
    "PCIe Queue (Host)",
    "PCIe Queue (Device)",
    "PCIe Wire",
    "PCIe Xfer (Data)",
    "PCIe Xfer (CQ)",
    # NAND/ONFI timing
    "ONFI Xfer",
    "ONFI Service",
    "Array Exec",
    # Energy, status, and maintenance statistics
    "Energy for req (μJ)",
    "Energy for persistant storage (μJ)",
    "Status",
    "GC Count",
    "GC Relocated Pages",
    "GC Erased Blocks",
    "Write Amplification",
)

RESPONSE_DATA_MESSAGE_TYPES = {
    MessageType.READ_RES_SEND_BACK.value,
    MessageType.SEARCH_RES_SEND_BACK.value,
    MessageType.COMPUTE_RES_SEND_BACK.value,
}

STATUS_MESSAGE_TYPES = {MessageType.REQ_COMP.value}
REQUEST_SIDE_DEVICE_TO_HOST_MESSAGE_TYPES = {
    MessageType.READ_REQ.value,
    MessageType.WRITE_REQ.value,
    MessageType.SEARCH_REQ.value,
    MessageType.COMPUTE_REQ.value,
    MessageType.STATIC_WRITE_REQ.value,
    MessageType.WRITE_DATA_REQ.value,
    MessageType.SEARCH_DATA_REQ.value,
    MessageType.COMPUTE_DATA_REQ.value,
    MessageType.STATIC_WRITE_DATA_REQ.value,
}

WRITE_LIKE_REQUEST_TYPES = {RequestType.WRITE.value, RequestType.STATIC_WRITE.value}
NON_MAPPING_REQUEST_TYPES = {
    RequestType.SEARCH.value,
    RequestType.COMPUTE.value,
    RequestType.STATIC_WRITE.value,
}
HOST_VISIBLE_TRANSACTION_TYPES = {
    TransactionType.USER_READ.value,
    TransactionType.USER_WRITE.value,
    TransactionType.USER_SEARCH.value,
    TransactionType.USER_COMPUTE.value,
    TransactionType.USER_STATIC_WRITE.value,
}
MAPPING_TRANSACTION_TYPES = {TransactionType.MAPPING_READ.value}


def _empty_mapping_resolution_counts() -> dict[str, int]:
    return {
        "cmt_hit": 0,
        "gmt_hit": 0,
        "metadata_hit": 0,
        "mapping_read": 0,
        "uncached_write": 0,
    }


def _zero_breakdown() -> dict[str, int]:
    data = {stage: 0 for stage in ALL_INTERVAL_STAGE_NAMES}
    data.update({stage: 0 for stage in RECONCILIATION_STAGE_NAMES})
    return data


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted((min(start, end), max(start, end)) for start, end in intervals)
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _merged_duration(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merge_intervals(intervals))


@dataclass
class RequestLatencyState:
    req_id: str
    trace_index: Optional[int]
    trace_time: Optional[int]
    req_type: str
    lha_start: Optional[int]
    size: Optional[int]
    stream_id: int
    sq_id: Optional[int]
    scheduled_time: Optional[int] = None
    req_init_time: Optional[int] = None
    sq_enter_time: Optional[int] = None
    first_host_send_time: Optional[int] = None
    host_completion_time: Optional[int] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    intervals: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {stage: [] for stage in ALL_INTERVAL_STAGE_NAMES}
    )
    persistence_intervals: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {stage: [] for stage in ALL_INTERVAL_STAGE_NAMES}
    )
    persistence_status: str = "not_applicable"
    persistence_completion_time: Optional[int] = None
    direct_media_write: bool = False
    mapping_resolution_counts: dict[str, int] = field(default_factory=_empty_mapping_resolution_counts)
    data_cache_status: str = "not_checked"
    energy_uj: float = 0.0          # per-request total energy (μJ), attributed via source_req
    persistence_energy_uj: float = 0.0  # energy from cache-flush/GC writebacks attributed to this request


class RequestLatencyRecorder:
    def __init__(self) -> None:
        self.engine = None
        self.trace_path: Optional[Path] = None
        self.requests: dict[str, RequestLatencyState] = {}
        self._pcie_messages: dict[int, dict[str, Any]] = {}
        self._mapping_waits: dict[tuple[str, str], int] = {}
        self._tsu_enqueue_times: dict[int, int] = {}
        self._backpressure_enqueue_times: dict[int, int] = {}
        self.maintenance_stats: dict[str, Any] = {
            "gc_count": 0,
            "static_wl_count": 0,
            "gc_relocated_pages": 0,
            "gc_erased_blocks": 0,
            "host_write_pages": 0,
            "physical_user_write_pages": 0,
            "physical_gc_write_pages": 0,
            "min_free_pool": None,
            "max_wear_skew": 0,
            "current_waiting_writes": 0,
            "max_waiting_writes": 0,
            "backpressure_enqueued": 0,
            "backpressure_retried": 0,
            "backpressure_wait_time": 0,
            "precondition": {},
            "planes": {},
        }

    def attach(self, engine: Any) -> None:
        self.engine = engine

    def set_trace_context(self, trace_path: str | Path) -> None:
        self.trace_path = Path(trace_path)

    def derive_report_path(self, root_dir: str | Path) -> Path:
        base_name = self.trace_path.stem if self.trace_path is not None else "request_latency"
        return Path(root_dir) / f"{base_name}_request_latency.json"

    def derive_csv_report_path(self, root_dir: str | Path) -> Path:
        base_name = self.trace_path.stem if self.trace_path is not None else "request_latency"
        return Path(root_dir) / f"{base_name}_request_latency.csv"

    def register_request(self, req: Request, scheduled_time: Optional[int] = None) -> None:
        rec = self._ensure_request(req)
        rec.scheduled_time = scheduled_time
        if req.type in (RequestType.WRITE, RequestType.STATIC_WRITE):
            rec.persistence_status = "pending_without_flush"
        if req.type == RequestType.WRITE and req.size is not None and req.size > 0:
            self.maintenance_stats["host_write_pages"] += ceil(req.size / SECTOR_PER_PAGE)

    def note_req_init_executed(self, req: Request, timestamp: int) -> None:
        rec = self._ensure_request(req)
        if rec.req_init_time is None:
            rec.req_init_time = timestamp

    def note_sq_entered(self, req: Request, timestamp: int) -> None:
        rec = self._ensure_request(req)
        if rec.sq_enter_time is None:
            rec.sq_enter_time = timestamp
        else:
            rec.sq_enter_time = min(rec.sq_enter_time, timestamp)
        if req.sq_id is not None:
            rec.sq_id = req.sq_id

    def note_host_sent(self, req: Request, timestamp: int) -> None:
        rec = self._ensure_request(req)
        if rec.first_host_send_time is not None:
            return
        rec.first_host_send_time = timestamp
        if rec.req_init_time is not None and timestamp > rec.req_init_time:
            self._append_interval(rec, "intervals", "host_dispatch", rec.req_init_time, timestamp, {"source": "host"})
        if rec.sq_enter_time is not None and timestamp > rec.sq_enter_time:
            self._append_interval(rec, "intervals", "host_sq_wait", rec.sq_enter_time, timestamp, {"source": "host"})

    def note_pcie_enqueued(
        self,
        message: Any,
        direction: str,
        timestamp: int,
        transfer_bytes: int,
    ) -> None:
        request_ids = self._request_ids_from_message(message)
        if not request_ids:
            return
        self._pcie_messages[id(message)] = {
            "direction": direction,
            "message_type": getattr(getattr(message, "type", None), "value", str(getattr(message, "type", ""))),
            "start": timestamp,
            "transfer_start": None,
            "transfer_bytes": transfer_bytes,
            "request_ids": request_ids,
            "pcie_phase": getattr(message, "_nvme_command_phase", None),
        }

    def note_pcie_transfer_started(self, message: Any, timestamp: int) -> None:
        info = self._pcie_messages.get(id(message))
        if info is not None:
            info["transfer_start"] = timestamp

    def note_pcie_delivered(self, message: Any, timestamp: int) -> None:
        info = self._pcie_messages.pop(id(message), None)
        if info is None:
            return
        stage = "pcie_host_to_device" if info["direction"] == "host_to_device" else "pcie_device_to_host"
        queue_stage = f"{stage}_queue_wait"
        wire_stage = f"{stage}_wire"
        transfer_start = info["transfer_start"]
        if transfer_start is None:
            transfer_start = info["start"]
        metadata = {
            "source": "pcie",
            "direction": info["direction"],
            "message_type": info["message_type"],
            "transfer_bytes": info["transfer_bytes"],
            "pcie_phase": info["pcie_phase"],
        }
        for req_id in info["request_ids"]:
            rec = self.requests.get(req_id)
            if rec is None:
                continue
            self._append_interval(
                rec,
                "intervals",
                stage,
                info["start"],
                timestamp,
                metadata,
            )
            self._append_interval(
                rec,
                "intervals",
                queue_stage,
                info["start"],
                transfer_start,
                metadata,
            )
            self._append_interval(
                rec,
                "intervals",
                wire_stage,
                transfer_start,
                timestamp,
                metadata,
            )

    def note_mapping_wait_start(self, req: Optional[Request], wait_key: str, timestamp: int) -> None:
        req_id = self._request_id(req)
        if req_id is None:
            return
        self._mapping_waits[(req_id, wait_key)] = timestamp

    def note_mapping_wait_end(self, req: Optional[Request], wait_key: str, timestamp: int) -> None:
        req_id = self._request_id(req)
        if req_id is None:
            return
        start = self._mapping_waits.pop((req_id, wait_key), None)
        if start is None:
            return
        rec = self.requests.get(req_id)
        if rec is None:
            return
        self._append_interval(
            rec,
            "intervals",
            "amu_mapping_wait",
            start,
            timestamp,
            {"source": "mapping_wait", "wait_key": wait_key},
        )

    def note_mapping_resolution(self, req: Optional[Request], resolution: str) -> None:
        req_id = self._request_id(req)
        if req_id is None:
            return
        rec = self.requests.get(req_id)
        if rec is None:
            return
        if resolution not in rec.mapping_resolution_counts:
            raise ValueError(f"Unsupported mapping resolution: {resolution}")
        rec.mapping_resolution_counts[resolution] += 1

    def note_data_cache_result(
        self,
        req: Optional[Request],
        *,
        hit_count: int,
        miss_count: int,
        blocked_count: int,
    ) -> None:
        req_id = self._request_id(req)
        if req_id is None:
            return
        rec = self.requests.get(req_id)
        if rec is None:
            return
        if blocked_count > 0:
            rec.data_cache_status = "waiting_for_write_data"
        elif hit_count > 0 and miss_count == 0:
            rec.data_cache_status = "full_hit"
        elif hit_count > 0:
            rec.data_cache_status = "partial_hit"
        elif miss_count > 0:
            rec.data_cache_status = "miss"
        else:
            rec.data_cache_status = "not_checked"

    def note_tsu_enqueued(self, tr: Transaction, timestamp: int) -> None:
        self._tsu_enqueue_times.setdefault(id(tr), timestamp)

    def note_tsu_dispatched(self, tr: Transaction, timestamp: int) -> None:
        txn_id = id(tr)
        # Consume the matching enqueue timestamp.  Keeping dispatched object
        # ids forever lets Python reuse an old id for a later transaction and
        # incorrectly suppresses that later transaction's TSU interval.
        start = self._tsu_enqueue_times.pop(txn_id, None)
        if start is None or timestamp <= start:
            return
        request_ids, scope = self._request_ids_from_transaction(tr)
        for req_id in request_ids:
            rec = self.requests.get(req_id)
            if rec is None:
                continue
            bucket = "persistence_intervals" if scope == "persistence" else "intervals"
            self._append_interval(
                rec,
                bucket,
                "tsu_queue_wait",
                start,
                timestamp,
                {"source": "tsu", "transaction_type": tr.type.value},
            )

    def note_phy_command_phase(
        self,
        transactions: list[Transaction],
        op_kind: str,
        start_time: int,
        finish_time: int,
        cmd_addr_time: int,
    ) -> None:
        cmd_end = min(finish_time, start_time + cmd_addr_time)
        for tr in transactions:
            self._record_transaction_interval(
                tr,
                "phy_cmd_addr",
                start_time,
                cmd_end,
                {"source": "phy", "transaction_type": tr.type.value, "op_kind": op_kind},
            )
            if finish_time > cmd_end and op_kind in ("write", "search", "compute"):
                self._record_transaction_interval(
                    tr,
                    "phy_data_in",
                    cmd_end,
                    finish_time,
                    {"source": "phy", "transaction_type": tr.type.value, "op_kind": op_kind},
                )

    def note_phy_channel_wait(
        self,
        transactions: list[Transaction],
        op_kind: str,
        transfer_kind: str,
        start_time: int,
        finish_time: int,
    ) -> None:
        if finish_time <= start_time:
            return
        for tr in transactions:
            self._record_transaction_interval(
                tr,
                "phy_channel_wait",
                start_time,
                finish_time,
                {
                    "source": "phy",
                    "transaction_type": tr.type.value,
                    "op_kind": op_kind,
                    "transfer_kind": transfer_kind,
                },
            )

    def note_phy_array_phase(
        self,
        transactions: list[Transaction],
        op_kind: str,
        start_time: int,
        finish_time: int,
    ) -> None:
        for tr in transactions:
            self._record_transaction_interval(
                tr,
                "phy_array_exec",
                start_time,
                finish_time,
                {"source": "phy", "transaction_type": tr.type.value, "op_kind": op_kind},
            )

    def note_phy_data_in_phase(
        self,
        transactions: list[Transaction],
        op_kind: str,
        start_time: int,
        finish_time: int,
    ) -> None:
        for tr in transactions:
            self._record_transaction_interval(
                tr,
                "phy_data_in",
                start_time,
                finish_time,
                {"source": "phy", "transaction_type": tr.type.value, "op_kind": op_kind},
            )

    def note_phy_data_out_phase(
        self,
        transactions: list[Transaction],
        op_kind: str,
        start_time: int,
        finish_time: int,
    ) -> None:
        for tr in transactions:
            self._record_transaction_interval(
                tr,
                "phy_data_out",
                start_time,
                finish_time,
                {"source": "phy", "transaction_type": tr.type.value, "op_kind": op_kind},
            )

    def note_request_completed(self, req: Request, timestamp: int) -> None:
        rec = self._ensure_request(req)
        rec.host_completion_time = timestamp
        rec.status = req.status
        rec.error_message = req.error_message
        req_type = req.type.value if hasattr(req.type, "value") else str(req.type)
        if req_type in WRITE_LIKE_REQUEST_TYPES and self._is_direct_media_write(req):
            rec.direct_media_write = True
            rec.persistence_status = "persisted"
            rec.persistence_completion_time = timestamp

    def note_persistence_completed(self, tr: Transaction, timestamp: int) -> None:
        request_ids, scope = self._request_ids_from_transaction(tr)
        if scope != "persistence":
            return
        for req_id in request_ids:
            rec = self.requests.get(req_id)
            if rec is None:
                continue
            rec.persistence_status = "persisted"
            if rec.persistence_completion_time is None or timestamp > rec.persistence_completion_time:
                rec.persistence_completion_time = timestamp

    def note_backpressure_enqueue(self, tr: Transaction, plane_id: int, timestamp: int) -> None:
        self._backpressure_enqueue_times[id(tr)] = timestamp
        self.maintenance_stats["backpressure_enqueued"] += 1
        current = int(self.maintenance_stats.get("current_waiting_writes", 0)) + 1
        self.maintenance_stats["current_waiting_writes"] = current
        self.maintenance_stats["max_waiting_writes"] = max(
            int(self.maintenance_stats.get("max_waiting_writes", 0)),
            current,
        )

    def note_backpressure_retry(
        self,
        tr: Transaction,
        plane_id: int,
        timestamp: int,
        *,
        submitted: bool,
    ) -> None:
        if not submitted:
            return
        start = self._backpressure_enqueue_times.pop(id(tr), None)
        if start is not None and timestamp >= start:
            self.maintenance_stats["backpressure_wait_time"] += timestamp - start
        self.maintenance_stats["backpressure_retried"] += 1
        self.maintenance_stats["current_waiting_writes"] = max(
            0,
            int(self.maintenance_stats.get("current_waiting_writes", 0)) - 1,
        )

    def note_gc_started(
        self,
        reason: str,
        plane_addr: Any,
        victim_block: int,
        *,
        valid_page_count: int,
        invalid_page_count: int,
    ) -> None:
        if reason == "static-wl":
            self.maintenance_stats["static_wl_count"] += 1
        else:
            self.maintenance_stats["gc_count"] += 1
        self.maintenance_stats["gc_relocated_pages"] += max(0, valid_page_count)

    def note_gc_erase_completed(self, addr: Any, wl_level: int) -> None:
        self.maintenance_stats["gc_erased_blocks"] += 1

    def note_physical_write(self, tr: Transaction) -> None:
        if tr.type == TransactionType.GC_WRITE:
            self.maintenance_stats["physical_gc_write_pages"] += 1
        elif tr.type in (TransactionType.USER_WRITE, TransactionType.USER_STATIC_WRITE):
            self.maintenance_stats["physical_user_write_pages"] += 1

    def note_plane_pool_snapshot(
        self,
        plane_addr: Any,
        *,
        free_pool_count: int,
        wear_skew: int,
        waiting_write_count: int,
    ) -> None:
        key = (
            f"ch{plane_addr.channel}.chip{plane_addr.chip}."
            f"die{plane_addr.die}.plane{plane_addr.plane}"
        )
        planes = self.maintenance_stats["planes"]
        plane_stats = planes.setdefault(
            key,
            {
                "min_free_pool": free_pool_count,
                "max_wear_skew": wear_skew,
                "max_waiting_writes": waiting_write_count,
            },
        )
        plane_stats["min_free_pool"] = min(plane_stats["min_free_pool"], free_pool_count)
        plane_stats["max_wear_skew"] = max(plane_stats["max_wear_skew"], wear_skew)
        plane_stats["max_waiting_writes"] = max(
            plane_stats["max_waiting_writes"],
            waiting_write_count,
        )
        min_free_pool = self.maintenance_stats["min_free_pool"]
        if min_free_pool is None:
            self.maintenance_stats["min_free_pool"] = free_pool_count
        else:
            self.maintenance_stats["min_free_pool"] = min(min_free_pool, free_pool_count)
        self.maintenance_stats["max_wear_skew"] = max(
            int(self.maintenance_stats.get("max_wear_skew", 0)),
            wear_skew,
        )

    def add_energy(self, transactions: list[Transaction], energy_uj: float) -> None:
        """将 PHY 操作的能耗归属到发起该事务的请求。"""
        for tr in transactions:
            request_ids, scope = self._request_ids_from_transaction(tr)
            for req_id in request_ids:
                rec = self.requests.get(req_id)
                if rec is None:
                    continue
                if scope == "persistence":
                    rec.persistence_energy_uj += energy_uj
                else:
                    rec.energy_uj += energy_uj

    def export(self) -> dict[str, Any]:
        requests_payload = []
        for rec in self._iter_sorted_requests():
            total_latency = self._total_latency(rec.req_init_time, rec.host_completion_time)
            host_breakdown = self._summarize_breakdown(rec.intervals, total_latency)
            persistence_total = self._total_latency(rec.req_init_time, rec.persistence_completion_time)
            persistence_breakdown = self._summarize_breakdown(rec.persistence_intervals, persistence_total)
            persistence_status = rec.persistence_status
            persistence_origin = "not_applicable"
            if rec.direct_media_write and rec.req_type in WRITE_LIKE_REQUEST_TYPES:
                persistence_status = "persisted"
                persistence_total = total_latency
                persistence_breakdown = dict(host_breakdown)
                persistence_origin = "host_media_path"
            elif persistence_status == "persisted":
                persistence_origin = "cache_flush"
            if rec.req_type in (RequestType.WRITE.value, RequestType.STATIC_WRITE.value):
                if rec.persistence_completion_time is None and persistence_status != "persisted":
                    persistence_status = "superseded_in_cache"
                    persistence_total = 0
                    persistence_breakdown = _zero_breakdown()
                    persistence_origin = "cache_superseded"
            else:
                persistence_status = "not_applicable"
                persistence_total = 0
                persistence_breakdown = _zero_breakdown()
                persistence_origin = "not_applicable"

            requests_payload.append(
                {
                    "req_id": rec.req_id,
                    "trace_index": rec.trace_index,
                    "trace_time": rec.trace_time,
                    "type": rec.req_type,
                    "stream_id": rec.stream_id,
                    "sq_id": rec.sq_id,
                    "lha_start": rec.lha_start,
                    "size": rec.size,
                    "status": rec.status,
                    "error_message": rec.error_message,
                    "scheduled_time": rec.scheduled_time,
                    "req_init_time": rec.req_init_time,
                    "host_completion_time": rec.host_completion_time,
                    "total_latency": total_latency,
                    "host_total_latency": total_latency,
                    "breakdown": host_breakdown,
                    "intervals": rec.intervals,
                    "mapping_resolution_counts": dict(rec.mapping_resolution_counts),
                    "energy_uj": rec.energy_uj,
                    "persistence_energy_uj": rec.persistence_energy_uj,
                    "data_cache_status": rec.data_cache_status,
                    "persistence_status": persistence_status,
                    "persistence_origin": persistence_origin,
                    "persistence_completion_time": rec.persistence_completion_time,
                    "persistence_total_latency": persistence_total,
                    "persistence_breakdown": persistence_breakdown,
                    "persistence_intervals": rec.persistence_intervals,
                }
            )

        return {
            "meta": {
                "trace_path": str(self.trace_path) if self.trace_path is not None else None,
                "trace_name": self.trace_path.name if self.trace_path is not None else None,
                "final_time": int(self.engine.current_time if self.engine is not None else 0),
                "request_count": len(requests_payload),
                "stage_names": list(ALL_INTERVAL_STAGE_NAMES),
                "maintenance": self._maintenance_summary(),
            },
            "requests": requests_payload,
        }

    def dump_json(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.export(), handle, ensure_ascii=False, indent=2)
        return path

    def export_csv_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for rec in self._iter_sorted_requests():
            rows.append(
                {
                    "Issue Time": self._csv_value(self._issue_time(rec)),
                    "REQ Type": rec.req_type,
                    "Finish Time": self._csv_value(rec.host_completion_time),
                    "Time in SQ": self._host_wait_time(rec),
                    "Cache Hit": self._csv_cache_hit_value(rec),
                    "Mapping": self._mapping_time(rec),
                    "Time in TSU": self._user_tsu_wait_time(rec),
                    "Backpressure Wait Time": self.maintenance_stats["backpressure_wait_time"],
                    "PCIe Xfer": self._pcie_request_send_time(rec),
                    "PCIe Queue (Host)": self._pcie_queue_time(rec, "host_to_device"),
                    "PCIe Queue (Device)": self._pcie_queue_time(rec, "device_to_host"),
                    "PCIe Wire": self._pcie_wire_time(rec),
                    "PCIe Xfer (Data)": self._pcie_data_return_time(rec),
                    "PCIe Xfer (CQ)": self._pcie_status_return_time(rec),
                    "ONFI Xfer": self._user_phy_transfer_time(rec),
                    "ONFI Service": self._user_phy_service_time(rec),
                    "Array Exec": self._user_phy_array_time(rec),
                    "Energy for req (μJ)": self._csv_energy_value(rec.energy_uj),
                    "Energy for persistant storage (μJ)": self._csv_energy_value(rec.persistence_energy_uj),
                    "Status": rec.status or "",
                    "GC Count": self.maintenance_stats["gc_count"],
                    "GC Relocated Pages": self.maintenance_stats["gc_relocated_pages"],
                    "GC Erased Blocks": self.maintenance_stats["gc_erased_blocks"],
                    "Write Amplification": self._csv_write_amplification_value(),
                }
            )
        return rows

    def dump_csv(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMN_NAMES))
            writer.writeheader()
            writer.writerows(self.export_csv_rows())
        return path

    def _ensure_request(self, req: Request) -> RequestLatencyState:
        req_id = self._request_id(req)
        if req_id is None:
            raise ValueError("Request missing report_req_id for latency reporting")
        if req_id not in self.requests:
            self.requests[req_id] = RequestLatencyState(
                req_id=req_id,
                trace_index=req.trace_index,
                trace_time=req.trace_time,
                req_type=req.type.value if hasattr(req.type, "value") else str(req.type),
                lha_start=req.lha_start,
                size=req.size,
                stream_id=req.stream_id,
                sq_id=req.sq_id,
            )
        rec = self.requests[req_id]
        rec.trace_index = req.trace_index
        rec.trace_time = req.trace_time
        rec.stream_id = req.stream_id
        rec.sq_id = req.sq_id
        rec.lha_start = req.lha_start
        rec.size = req.size
        return rec

    def _iter_sorted_requests(self) -> list[RequestLatencyState]:
        return sorted(
            self.requests.values(),
            key=lambda item: (
                item.trace_index if item.trace_index is not None else 10**9,
                item.req_id,
            ),
        )

    def _request_id(self, req: Optional[Request]) -> Optional[str]:
        if req is None:
            return None
        return req.report_req_id

    def _request_ids_from_message(self, message: Any) -> set[str]:
        payload = getattr(message, "payload", None)
        if not isinstance(payload, dict):
            return set()
        req = payload.get("req")
        req_id = self._request_id(req)
        if req_id is not None:
            return {req_id}
        origin_ids = payload.get("origin_request_ids")
        if origin_ids is None:
            return set()
        return {str(req_id) for req_id in origin_ids if req_id}

    def _request_ids_from_transaction(self, tr: Transaction) -> tuple[set[str], str]:
        req_id = self._request_id(tr.source_req)
        if req_id is not None:
            return {req_id}, "host"
        origin_ids = {str(req_id) for req_id in getattr(tr, "report_origin_request_ids", []) if req_id}
        if origin_ids:
            return origin_ids, "persistence"
        return set(), "host"

    def _is_direct_media_write(self, req: Request) -> bool:
        if getattr(req, "cache_forced_bypass", False):
            return True
        runtime = getattr(getattr(self.engine, "config", None), "runtime", None)
        return bool(getattr(runtime, "cache_bypass", False))

    def _append_interval(
        self,
        rec: RequestLatencyState,
        bucket_name: str,
        stage: str,
        start: int,
        end: int,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if end < start:
            start, end = end, start
        if end == start:
            return
        bucket = getattr(rec, bucket_name)
        bucket[stage].append({"start": int(start), "end": int(end), **(metadata or {})})

    def _record_transaction_interval(
        self,
        tr: Transaction,
        stage: str,
        start: int,
        end: int,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        request_ids, scope = self._request_ids_from_transaction(tr)
        if not request_ids:
            return
        bucket_name = "persistence_intervals" if scope == "persistence" else "intervals"
        for req_id in request_ids:
            rec = self.requests.get(req_id)
            if rec is None:
                continue
            self._append_interval(rec, bucket_name, stage, start, end, metadata)

    def _summarize_breakdown(
        self,
        interval_map: dict[str, list[dict[str, Any]]],
        total_latency: int,
    ) -> dict[str, int]:
        summary = _zero_breakdown()
        all_intervals: list[tuple[int, int]] = []
        for stage in ALL_INTERVAL_STAGE_NAMES:
            raw_intervals = [(item["start"], item["end"]) for item in interval_map.get(stage, [])]
            summary[stage] = _merged_duration(raw_intervals)
            if stage in BASE_STAGE_NAMES:
                all_intervals.extend(raw_intervals)
        union_duration = _merged_duration(all_intervals)
        base_sum = sum(summary[stage] for stage in BASE_STAGE_NAMES)
        summary["overlap_latency"] = max(0, base_sum - union_duration)
        summary["untracked_latency"] = max(0, total_latency - union_duration)
        return summary

    def _total_latency(self, start: Optional[int], end: Optional[int]) -> int:
        if start is None or end is None or end < start:
            return 0
        return int(end - start)

    def _issue_time(self, rec: RequestLatencyState) -> Optional[int]:
        if rec.req_init_time is not None:
            return rec.req_init_time
        if rec.scheduled_time is not None:
            return rec.scheduled_time
        return rec.trace_time

    def _host_wait_time(self, rec: RequestLatencyState) -> int:
        return self._merged_stage_durations(
            rec.intervals["host_dispatch"] + rec.intervals["host_sq_wait"]
        )

    def _pcie_request_send_time(self, rec: RequestLatencyState) -> int:
        # Report the measured request-side PCIe interval directly.  Deriving
        # this field as the residual of end-to-end latency makes unrelated,
        # untracked controller/channel gaps appear as PCIe time when stages
        # overlap or contend.
        return self._raw_request_side_pcie_time(rec)

    def _mapping_time(self, rec: RequestLatencyState) -> int:
        mapping_end = self._mapping_phase_end(rec)
        if mapping_end is None:
            return 0
        return max(0, mapping_end - self._request_side_pcie_end(rec))

    def _user_tsu_wait_time(self, rec: RequestLatencyState) -> int:
        return self._transaction_stage_duration(
            rec.intervals["tsu_queue_wait"],
            HOST_VISIBLE_TRANSACTION_TYPES,
        )

    def _user_phy_transfer_time(self, rec: RequestLatencyState) -> int:
        intervals = []
        for stage in (
            "phy_channel_wait",
            "phy_cmd_addr",
            "phy_data_in",
            "phy_data_out",
        ):
            intervals.extend(
                interval
                for interval in rec.intervals[stage]
                if interval.get("transaction_type") in HOST_VISIBLE_TRANSACTION_TYPES
            )
        return self._merged_stage_durations(intervals)

    def _user_phy_service_time(self, rec: RequestLatencyState) -> int:
        intervals = []
        for stage in ("phy_cmd_addr", "phy_data_in", "phy_data_out"):
            intervals.extend(
                interval
                for interval in rec.intervals[stage]
                if interval.get("transaction_type") in HOST_VISIBLE_TRANSACTION_TYPES
            )
        return self._merged_stage_durations(intervals)

    def _user_phy_array_time(self, rec: RequestLatencyState) -> int:
        return self._transaction_stage_duration(
            rec.intervals["phy_array_exec"],
            HOST_VISIBLE_TRANSACTION_TYPES,
        )

    def _merged_stage_durations(self, intervals: list[dict[str, Any]]) -> int:
        return _merged_duration([(item["start"], item["end"]) for item in intervals])

    def _stage_end(
        self,
        intervals: list[dict[str, Any]],
        allowed_transaction_types: Optional[set[str]] = None,
    ) -> Optional[int]:
        ends = [
            item["end"]
            for item in intervals
            if allowed_transaction_types is None
            or item.get("transaction_type") in allowed_transaction_types
        ]
        return max(ends) if ends else None

    def _stage_start(
        self,
        intervals: list[dict[str, Any]],
        allowed_transaction_types: Optional[set[str]] = None,
    ) -> Optional[int]:
        starts = [
            item["start"]
            for item in intervals
            if allowed_transaction_types is None
            or item.get("transaction_type") in allowed_transaction_types
        ]
        return min(starts) if starts else None

    def _transaction_stage_duration(
        self,
        intervals: list[dict[str, Any]],
        allowed_transaction_types: set[str],
    ) -> int:
        filtered = [
            (item["start"], item["end"])
            for item in intervals
            if item.get("transaction_type") in allowed_transaction_types
        ]
        return _merged_duration(filtered)

    def _request_side_pcie_intervals(self, rec: RequestLatencyState) -> list[dict[str, Any]]:
        request_intervals = list(rec.intervals["pcie_host_to_device"])
        request_intervals.extend(
            interval
            for interval in rec.intervals["pcie_device_to_host"]
            if interval.get("message_type") in REQUEST_SIDE_DEVICE_TO_HOST_MESSAGE_TYPES
        )
        return request_intervals

    def _raw_request_side_pcie_time(self, rec: RequestLatencyState) -> int:
        return self._merged_stage_durations(self._request_side_pcie_intervals(rec))

    def _pcie_queue_time(self, rec: RequestLatencyState, direction: str) -> int:
        return self._merged_stage_durations(
            rec.intervals[f"pcie_{direction}_queue_wait"]
        )

    def _pcie_wire_time(self, rec: RequestLatencyState) -> int:
        return self._merged_stage_durations(
            rec.intervals["pcie_host_to_device_wire"]
            + rec.intervals["pcie_device_to_host_wire"]
        )

    def _request_side_pcie_end(self, rec: RequestLatencyState) -> int:
        intervals = self._request_side_pcie_intervals(rec)
        if not intervals:
            return self._issue_time(rec) or 0
        return max(item["end"] for item in intervals)

    def _mapping_phase_end(self, rec: RequestLatencyState) -> Optional[int]:
        mapping_intervals = list(rec.intervals["amu_mapping_wait"])
        for stage in (
            "tsu_queue_wait",
            "phy_channel_wait",
            "phy_cmd_addr",
            "phy_data_in",
            "phy_array_exec",
            "phy_data_out",
        ):
            mapping_intervals.extend(
                interval
                for interval in rec.intervals[stage]
                if interval.get("transaction_type") in MAPPING_TRANSACTION_TYPES
            )
        if not mapping_intervals:
            return None
        return max(item["end"] for item in mapping_intervals)

    def _first_user_command_start(self, rec: RequestLatencyState) -> Optional[int]:
        return self._stage_start(
            rec.intervals["phy_cmd_addr"],
            HOST_VISIBLE_TRANSACTION_TYPES,
        )

    def _first_user_submit_time(
        self,
        rec: RequestLatencyState,
        fallback: int,
    ) -> int:
        submit_time = self._stage_start(
            rec.intervals["tsu_queue_wait"],
            HOST_VISIBLE_TRANSACTION_TYPES,
        )
        return fallback if submit_time is None else submit_time

    def _csv_cache_hit_value(self, rec: RequestLatencyState) -> str:
        if rec.req_type in NON_MAPPING_REQUEST_TYPES:
            return "/"
        if rec.req_type == RequestType.READ.value and rec.data_cache_status == "full_hit":
            return "Yes"
        total_mapping_lookups = sum(rec.mapping_resolution_counts.values())
        if total_mapping_lookups == 0:
            return "No"
        if rec.mapping_resolution_counts["cmt_hit"] == total_mapping_lookups:
            return "Yes"
        return "No"

    def _cache_hit_value(self, rec: RequestLatencyState) -> str:
        if rec.req_type in NON_MAPPING_REQUEST_TYPES:
            return "/"
        total_mapping_lookups = sum(rec.mapping_resolution_counts.values())
        if total_mapping_lookups == 0:
            return "No"
        if rec.mapping_resolution_counts["cmt_hit"] == total_mapping_lookups:
            return "Yes"
        return "No"

    def _pcie_status_return_time(
        self,
        rec: RequestLatencyState,
    ) -> int:
        return self._filtered_interval_duration(
            rec.intervals["pcie_device_to_host"],
            STATUS_MESSAGE_TYPES,
        )

    def _pcie_data_return_time(
        self,
        rec: RequestLatencyState,
    ) -> int:
        explicit_data_latency, has_explicit_response = self._filtered_interval_duration(
            rec.intervals["pcie_device_to_host"],
            RESPONSE_DATA_MESSAGE_TYPES,
            return_response_presence=True,
        )
        if rec.req_type in WRITE_LIKE_REQUEST_TYPES:
            return 0
        if has_explicit_response or rec.status != REQUEST_STATUS_SUCCESS:
            return explicit_data_latency
        return self._estimate_response_payload_latency(rec)

    def _estimate_response_payload_latency(self, rec: RequestLatencyState) -> int:
        if rec.size is None or rec.size <= 0:
            return 0
        if PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS <= 0:
            raise ValueError("PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS must be positive")
        payload_bytes = rec.size * SECTOR_SIZE_BYTES
        packet_count = ceil(payload_bytes / PCIE_TLP_MAX_PAYLOAD_BYTES)
        transfer_bytes = (
            payload_bytes + packet_count * PCIE_TLP_PACKET_OVERHEAD_BYTES
        )
        return ceil(transfer_bytes / PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS)

    def _filtered_interval_duration(
        self,
        intervals: list[dict[str, Any]],
        allowed_message_types: set[str],
        *,
        return_response_presence: bool = False,
    ) -> int | tuple[int, bool]:
        selected: list[tuple[int, int]] = []
        has_response_data = False
        for interval in intervals:
            message_type = interval.get("message_type")
            if message_type not in allowed_message_types:
                continue
            selected.append((interval["start"], interval["end"]))
            if message_type in RESPONSE_DATA_MESSAGE_TYPES:
                has_response_data = True
        duration = _merged_duration(selected)
        if return_response_presence:
            return duration, has_response_data
        return duration

    def _csv_value(self, value: Optional[int]) -> int | str:
        if value is None:
            return ""
        return value

    @staticmethod
    def _csv_energy_value(value: float) -> str:
        return f"{value:.2f}"

    def _maintenance_summary(self) -> dict[str, Any]:
        summary = dict(self.maintenance_stats)
        summary["write_amplification"] = self._write_amplification()
        if summary["min_free_pool"] is None:
            summary["min_free_pool"] = 0
        return summary

    def _write_amplification(self) -> float:
        host_pages = int(self.maintenance_stats.get("host_write_pages", 0))
        if host_pages <= 0:
            return 0.0
        physical_pages = int(self.maintenance_stats.get("physical_user_write_pages", 0))
        physical_pages += int(self.maintenance_stats.get("physical_gc_write_pages", 0))
        return physical_pages / host_pages

    def _csv_write_amplification_value(self) -> str:
        return f"{self._write_amplification():.4f}"
