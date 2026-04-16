# -*- coding: utf-8 -*-
"""事件时间线记录器。

在不修改 Host/HIL/FTL/PHY 现有代码的前提下，通过 Engine 与 PHY 的运行时 hook
收集 request 与 transaction 的阶段时间点，输出可视化所需 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .common import EventType, MessageType


REQ_PHASE_ORDER = ["REQ_INIT", "DELIVER", "DATA", "REQ_COMP"]
TXN_PHASE_ORDER = ["dispatch", "CMP_TRANSFERED", "COMPLETE", "DATA_TRANSFERED"]

CMD_TRANSFER_EVENTS = {
    EventType.PHY_READ_CMD_TRANSFERRED,
    EventType.PHY_WRITE_CMD_TRANSFERRED,
    EventType.PHY_ERASE_CMD_TRANSFERRED,
    EventType.PHY_SEARCH_CMD_TRANSFERRED,
    EventType.PHY_COMPUTE_CMD_TRANSFERRED,
}

CHIP_COMPLETE_EVENTS = {
    EventType.PHY_CHIP_READ_COMPLETE,
    EventType.PHY_CHIP_WRITE_COMPLETE,
    EventType.PHY_CHIP_ERASE_COMPLETE,
    EventType.PHY_CHIP_SEARCH_COMPLETE,
    EventType.PHY_CHIP_COMPUTE_COMPLETE,
}

DATA_TRANSFER_EVENTS = {
    EventType.PHY_READ_DATA_TRANSFERRED,
    EventType.PHY_SEARCH_DATA_TRANSFERRED,
    EventType.PHY_COMPUTE_DATA_TRANSFERRED,
}


@dataclass
class ReqRecord:
    req_key: str
    req_type: str
    start_lha: Optional[int]
    size: Optional[int]
    stream_id: int
    phases: dict[str, int] = field(default_factory=dict)


@dataclass
class TxnRecord:
    txn_key: str
    txn_type: str
    source_req: Optional[str]
    accessed_lpa: Optional[int]
    accessed_address: dict[str, Any]
    channel: int
    chip: int
    phases: dict[str, int] = field(default_factory=dict)


class TimelineRecorder:
    """收集仿真执行阶段事件并导出为可视化 JSON。"""

    def __init__(self) -> None:
        self.engine = None
        self._orig_register_event: Optional[Callable[..., Any]] = None
        self._orig_execute_event: Optional[Callable[..., Any]] = None
        self._orig_send_command_to_chip: Optional[Callable[..., Any]] = None

        self.req_records: dict[str, ReqRecord] = {}
        self.txn_records: dict[str, TxnRecord] = {}

    def attach(self, engine: Any) -> None:
        """将 recorder 附加到 engine 与 phy。"""
        self.engine = engine

        self._orig_register_event = engine.Register_event
        self._orig_execute_event = engine.Execute_event

        def _patched_register_event(event_type, target, param, scheduled_time):
            return self._orig_register_event(event_type, target, param, scheduled_time)

        def _patched_execute_event():
            event = engine.event_queue.get()
            if event.ignored:
                return
            engine.current_time = event.time
            event.target.execute(event)
            self._capture_after_execute(event)

        engine.Register_event = _patched_register_event
        engine.Execute_event = _patched_execute_event

        phy = engine.device.phy
        self._orig_send_command_to_chip = phy.send_command_to_chip

        def _patched_send_command_to_chip(chip_id, transactions, suspension_required):
            now = engine.current_time
            for tr in transactions:
                self._mark_txn_phase(tr, "dispatch", now)
            return self._orig_send_command_to_chip(chip_id, transactions, suspension_required)

        phy.send_command_to_chip = _patched_send_command_to_chip

    def _capture_after_execute(self, event: Any) -> None:
        """在 event.execute 之后提取阶段信息。"""
        now = int(event.time)

        if event.type == EventType.REQ_INIT:
            req = event.param.get("req") if isinstance(event.param, dict) else None
            if req is not None:
                self._mark_req_phase(req, "REQ_INIT", now)
            return

        if event.type == EventType.DELIVER:
            message = event.param.get("message") if isinstance(event.param, dict) else None
            if message is None:
                return
            msg_type = message.type
            payload = message.payload or {}
            req = payload.get("req")

            if msg_type in {
                MessageType.WRITE_REQ,
                MessageType.READ_REQ,
                MessageType.SEARCH_REQ,
                MessageType.COMPUTE_REQ,
                MessageType.STATIC_WRITE_REQ,
            }:
                if req is not None:
                    self._mark_req_phase(req, "DELIVER", now)
            elif msg_type in {
                MessageType.WRITE_DATA,
                MessageType.SEARCH_DATA,
                MessageType.COMPUTE_DATA,
                MessageType.STATIC_WRITE_DATA,
            }:
                if req is not None:
                    self._mark_req_phase(req, "DATA", now)
            elif msg_type == MessageType.REQ_COMP:
                if req is not None:
                    self._mark_req_phase(req, "REQ_COMP", now)
            return

        transactions = []
        if isinstance(event.param, dict):
            transactions = event.param.get("transactions", [])

        if event.type in CMD_TRANSFER_EVENTS:
            for tr in transactions:
                self._mark_txn_phase(tr, "CMP_TRANSFERED", now)
            return

        if event.type in CHIP_COMPLETE_EVENTS:
            for tr in transactions:
                self._mark_txn_phase(tr, "COMPLETE", now)
            return

        if event.type in DATA_TRANSFER_EVENTS:
            for tr in transactions:
                self._mark_txn_phase(tr, "DATA_TRANSFERED", now)
            return

    def _req_key(self, req: Any) -> str:
        return f"req_{id(req)}"

    def _txn_key(self, tr: Any) -> str:
        return f"txn_{id(tr)}"

    def _stream_id_of_req(self, req: Any) -> int:
        if hasattr(req, "stream_id") and getattr(req, "stream_id") is not None:
            return int(getattr(req, "stream_id"))
        if getattr(req, "sq_id", None) is not None:
            return int(req.sq_id)
        return 0

    def _mark_req_phase(self, req: Any, phase: str, t: int) -> None:
        key = self._req_key(req)
        if key not in self.req_records:
            self.req_records[key] = ReqRecord(
                req_key=key,
                req_type=str(getattr(req, "type", "")),
                start_lha=getattr(req, "lha_start", None),
                size=getattr(req, "size", None),
                stream_id=self._stream_id_of_req(req),
            )

        rec = self.req_records[key]
        rec.stream_id = self._stream_id_of_req(req)
        if phase not in rec.phases:
            rec.phases[phase] = t

    def _mark_txn_phase(self, tr: Any, phase: str, t: int) -> None:
        key = self._txn_key(tr)

        source_req = getattr(tr, "source_req", None)
        source_req_key = self._req_key(source_req) if source_req is not None else None
        addr = getattr(tr, "address", None)
        addr_dict = {
            "channel": getattr(addr, "channel", -1),
            "chip": getattr(addr, "chip", -1),
            "die": getattr(addr, "die", -1),
            "plane": getattr(addr, "plane", -1),
            "sub_plane": getattr(addr, "sub_plane", -1),
            "page": getattr(addr, "page", -1),
        }

        if key not in self.txn_records:
            self.txn_records[key] = TxnRecord(
                txn_key=key,
                txn_type=str(getattr(tr, "type", "")),
                source_req=source_req_key,
                accessed_lpa=(None if getattr(tr, "lpa", -1) == -1 else getattr(tr, "lpa", None)),
                accessed_address=addr_dict,
                channel=int(addr_dict["channel"]),
                chip=int(addr_dict["chip"]),
            )

        rec = self.txn_records[key]
        if phase not in rec.phases:
            rec.phases[phase] = t

    def _segments_from_phases(self, phases: dict[str, int], order: list[str]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        existing = [p for p in order if p in phases]
        for i, phase in enumerate(existing):
            start = int(phases[phase])
            if i + 1 < len(existing):
                end = int(phases[existing[i + 1]])
            else:
                end = start + 1
            if end <= start:
                end = start + 1
            segments.append({"phase": phase, "start": start, "end": end})
        return segments

    def export(self) -> dict[str, Any]:
        requests = []
        for rec in self.req_records.values():
            requests.append(
                {
                    "req_key": rec.req_key,
                    "type": rec.req_type,
                    "start_lha": rec.start_lha,
                    "size": rec.size,
                    "stream_id": rec.stream_id,
                    "phases": rec.phases,
                    "segments": self._segments_from_phases(rec.phases, REQ_PHASE_ORDER),
                }
            )

        transactions = []
        for rec in self.txn_records.values():
            transactions.append(
                {
                    "txn_key": rec.txn_key,
                    "type": rec.txn_type,
                    "source_req": rec.source_req,
                    "accessed_lpa": rec.accessed_lpa,
                    "accessed_address": rec.accessed_address,
                    "channel": rec.channel,
                    "chip": rec.chip,
                    "phases": rec.phases,
                    "segments": self._segments_from_phases(rec.phases, TXN_PHASE_ORDER),
                }
            )

        return {
            "meta": {
                "final_time": int(self.engine.current_time if self.engine is not None else 0),
                "request_count": len(requests),
                "transaction_count": len(transactions),
            },
            "requests": requests,
            "transactions": transactions,
        }

    def dump_json(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        payload = self.export()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
