# -*- coding: utf-8 -*-
"""Flash 物理层：芯片/Die 状态管理与事件驱动命令执行。

SEARCH/COMPUTE 专属操作由子类或独立路径扩展，此处仅实现 READ/WRITE/ERASE。
"""

from dataclasses import dataclass, field
from doctest import debug
from enum import Enum
from typing import Dict, List, Tuple, Optional, Callable
from .common import *


# ── Helpers ───────────────────────────────────────────────────────────────────

def _op_kind(trans_type: TransactionType) -> str:
    """Derive low-level operation kind from transaction type string.

    Transaction types are queue-key strings such as 'user_read', 'gc_write',
    'gc_erase', 'mapping_read', etc.
    """
    t = trans_type.value.lower()
    if "erase" in t:
        return "erase"
    if "write" in t:
        return "write"
    if "search" in t:
        return "search"
    if "compute" in t:
        return "compute"
    return "read"


def _valid_plane_count(plane_count: int) -> int:
    return max(1, min(int(plane_count), 4))


def _ceil_two_unit_duration(payload_bytes: int, channel_width_bytes: int, two_unit_time: int) -> int:
    if payload_bytes <= 0:
        return 0
    units = (payload_bytes + channel_width_bytes * 2 - 1) // (channel_width_bytes * 2)
    return max(1, units * two_unit_time)


def onfi_data_in_duration(payload_bytes: int, timing: Optional[OnfiTimingConfig] = None) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    return _ceil_two_unit_duration(
        payload_bytes,
        timing.channel_width_bytes,
        timing.two_unit_data_in_time,
    )


def onfi_read_data_out_setup_duration(
    plane_count: int = 1,
    timing: Optional[OnfiTimingConfig] = None,
) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    planes = _valid_plane_count(plane_count)
    base = timing.t_rpre + timing.t_dqsre
    if planes == 1:
        return base
    per_extra_plane = timing.t_rhw + 6 * timing.t_wc + timing.t_ccs + timing.t_rpre + timing.t_dqsre
    return base + (planes - 1) * per_extra_plane


def onfi_data_out_duration(
    payload_bytes: int,
    plane_count: int = 1,
    timing: Optional[OnfiTimingConfig] = None,
) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    payload_time = _ceil_two_unit_duration(
        payload_bytes,
        timing.channel_width_bytes,
        timing.two_unit_data_out_time,
    )
    if payload_time == 0:
        return 0
    return onfi_read_data_out_setup_duration(plane_count, timing) + payload_time


def onfi_read_command_duration(
    plane_count: int = 1,
    timing: Optional[OnfiTimingConfig] = None,
) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    planes = _valid_plane_count(plane_count)
    return (
        timing.t_cs
        + planes * 6 * timing.t_wc
        + (planes - 1) * timing.t_dbsy
        + timing.t_wb
        + timing.t_rr
    )


def onfi_program_command_duration(
    plane_count: int = 1,
    timing: Optional[OnfiTimingConfig] = None,
) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    planes = _valid_plane_count(plane_count)
    final_plane = 6 * timing.t_wc + timing.t_adl + timing.t_wpst + timing.t_wpsth + timing.t_wb
    if planes == 1:
        return timing.t_cs + final_plane
    intermediate = 5 * timing.t_wc + timing.t_adl + timing.t_wpst + timing.t_cals + timing.t_wb
    return timing.t_cs + (planes - 1) * (intermediate + timing.t_dbsy) + final_plane


def onfi_erase_command_duration(
    plane_count: int = 1,
    timing: Optional[OnfiTimingConfig] = None,
) -> int:
    timing = timing or DEFAULT_ONFI_TIMING
    planes = _valid_plane_count(plane_count)
    per_plane = 4 * timing.t_wc + timing.t_wb
    return timing.t_cs + per_plane + (planes - 1) * (timing.t_dbsy + per_plane)


# ── Data structures ───────────────────────────────────────────────────────────

class ChannelTransferKind(Enum):
    COMMAND = "command"
    USER_DATA_IN = "user_data_in"
    GC_WRITE_DATA_IN = "gc_write_data_in"
    MAPPING_DATA_OUT = "mapping_data_out"
    USER_DATA_OUT = "user_data_out"
    STATIC_RESULT_DATA_OUT = "static_result_data_out"
    GC_READ_DATA_OUT = "gc_read_data_out"


CHANNEL_TRANSFER_PRIORITY = {
    ChannelTransferKind.COMMAND: 0,
    ChannelTransferKind.USER_DATA_IN: 1,
    ChannelTransferKind.GC_WRITE_DATA_IN: 2,
    ChannelTransferKind.MAPPING_DATA_OUT: 3,
    ChannelTransferKind.USER_DATA_OUT: 4,
    ChannelTransferKind.STATIC_RESULT_DATA_OUT: 5,
    ChannelTransferKind.GC_READ_DATA_OUT: 6,
}

DATA_OUT_TRANSFER_KINDS = {
    ChannelTransferKind.MAPPING_DATA_OUT,
    ChannelTransferKind.USER_DATA_OUT,
    ChannelTransferKind.STATIC_RESULT_DATA_OUT,
    ChannelTransferKind.GC_READ_DATA_OUT,
}

DATA_IN_TRANSFER_KINDS = {
    ChannelTransferKind.USER_DATA_IN,
    ChannelTransferKind.GC_WRITE_DATA_IN,
}


@dataclass
class ChannelTransferTask:
    kind: ChannelTransferKind
    channel_id: int
    chip_id: Tuple[int, int]
    die_id: int
    transactions: list
    total_duration: int
    op_kind: str
    payload_bytes: int = 0
    remaining_duration: int = 0
    start_time: Optional[int] = None
    finish_time: Optional[int] = None
    completion_event: Optional[SimEvent] = None
    queue_enter_time: Optional[int] = None
    sequence: int = 0
    priority: int = field(init=False)

    def __post_init__(self) -> None:
        self.priority = CHANNEL_TRANSFER_PRIORITY[self.kind]
        if self.remaining_duration <= 0:
            self.remaining_duration = self.total_duration

    @property
    def is_data_out(self) -> bool:
        return self.kind in DATA_OUT_TRANSFER_KINDS

    @property
    def is_data_in(self) -> bool:
        return self.kind in DATA_IN_TRANSFER_KINDS


class ActiveCommandInfo:
    """Bundles an operation kind with its pending transactions.
    """

    def __init__(self, cmd_type: str, transactions: list):
        self.cmd_type: str = cmd_type          # "read" | "write" | "erase" | "search" | "compute"
        self.transactions: list = transactions


class DieBKE:
    """Die-level book-keeping entry.
    """

    def __init__(self, die_id: int):
        self.die_id: int = die_id
        self.status: DieStatus = DieStatus.IDLE
        self.active_command: Optional[ActiveCommandInfo] = None
        self.suspended_command: Optional[ActiveCommandInfo] = None
        self.expected_finish_time: int = 0
        self._remaining_on_suspend: int = 0    # time left when suspend happened

    def prepare_suspend(self, current_time: int) -> None:
        """Save active command to suspended slot and record remaining time."""
        self._remaining_on_suspend = max(0, self.expected_finish_time - current_time)
        if self.active_command:
            for tr in self.active_command.transactions:
                if tr.exec_event is not None:
                    tr.exec_event.ignored = True
                    tr.exec_event = None
        self.suspended_command = self.active_command
        self.active_command = None

    def prepare_resume(self) -> int:
        """Restore suspended command to active; return remaining execution time."""
        self.active_command = self.suspended_command
        self.suspended_command = None
        return self._remaining_on_suspend

    def clear_command(self) -> None:
        """Mark all active transactions completed and release the command slot."""
        if self.active_command:
            for tr in self.active_command.transactions:
                tr.completed = True
        self.active_command = None


class ChipBKE:
    """Chip-level book-keeping entry. TSU 直接读取此类的字段来做调度决策。
    """

    def __init__(self, chip_id: Tuple[int, int]):
        self.chip_id: Tuple[int, int] = chip_id
        self.status: ChipStatus = ChipStatus.IDLE
        self.EnableWriteSuspend: bool = True
        self.EnableEraseSuspend: bool = True
        self.HasSuspendedCommands: bool = False
        self.Expected_Finish_Time: int = 0
        self.No_of_active_dies: int = 0
        self.dies: Dict[int, DieBKE] = {
            die_id: DieBKE(die_id) for die_id in range(DIE_PER_CHIP)
        }
        # Transactions whose chip-internal read finished; waiting for data-out transfer
        self._has_data_waiting: bool = False

    def get_die_bke(self, die_id: int) -> DieBKE:
        return self.dies[die_id]

class PageType(Enum):
    MAPPING = "MAPPING"
    USER = "USER"

@dataclass
class PageData:
    valid_bitmap: list[int] = field(default_factory=list)
    data: list[int] = field(default_factory=list)
    lpa: int = INVALID_LPA
    mvpn: int = INVALID_MVPN
    function: Optional[PageType] = None

    def __repr__(self) -> str:
        return (
            "<PageData "
            f"valid_bitmap_len={len(self.valid_bitmap)} "
            f"data_len={len(self.data)} "
            f"lpa={self.lpa} "
            f"mvpn={self.mvpn} "
            f"function={self.function.name if self.function is not None else None}"
            ">"
        )


# ── PHY class ─────────────────────────────────────────────────────────────────

class PHY():
    """Flash 物理层。接收 TSU 下发的命令（send_command_to_chip），通过仿真事件驱动 chip/channel 状态机，事务完成时通过回调通知 TSU 及上层。
    """
    def __init__(
        self,
        onfi_timing: Optional[OnfiTimingConfig] = None,
        cim_geometry=None,
    ):
        if not QUIET:
            print("Initializing PHY...")
        self._construction_valid: bool = False
        self.onfi_timing: OnfiTimingConfig = onfi_timing or DEFAULT_ONFI_TIMING
        self.wl_per_string = (
            cim_geometry.wl_per_string if cim_geometry is not None else WL_PER_STRING
        )
        self.bl_per_plane = (
            cim_geometry.bl_per_plane if cim_geometry is not None else BL_PER_PLANE
        )
        self.search_input_bits_per_wl = (
            cim_geometry.search_input_bits_per_wl
            if cim_geometry is not None
            else SEARCH_INPUT_BITS_PER_WL
        )
        self.search_match_bits_per_bl = (
            cim_geometry.search_match_bits_per_bl
            if cim_geometry is not None
            else SEARCH_MATCH_BITS_PER_BL
        )
        self.compute_input_bits_per_sl = (
            cim_geometry.compute_input_bits_per_sl
            if cim_geometry is not None
            else COMPUTE_INPUT_BITS_PER_SL
        )
        self.compute_accumulator_bits = (
            cim_geometry.compute_accumulator_bits
            if cim_geometry is not None
            else COMPUTE_ACCUMULATOR_BITS
        )
        self._channel_busy: List[bool] = [False] * CHANNEL_NO
        self._active_transfers: List[Optional[ChannelTransferTask]] = [None] * CHANNEL_NO
        self._pending_transfers: List[List[ChannelTransferTask]] = [[] for _ in range(CHANNEL_NO)]
        self._transfer_sequence: int = 0
        self._chip_bkes: Dict[Tuple[int, int], ChipBKE] = {
            (channel_id, chip_id): ChipBKE((channel_id, chip_id)) for channel_id in range(CHANNEL_NO) for chip_id in range(CHIP_PER_CHANNEL)
        }

        # Callback signal lists
        self._channel_idle_cbs: List[Callable[[int], None]] = []
        self._chip_idle_cbs: List[Callable[[Tuple[int, int]], None]] = []
        self._transaction_serviced_cbs: List[Callable] = []
        # _storage[channel][chip][die][plane][block][page] = list of SECTOR_PER_PAGE sector data (int or None)
        self._storage: List = [
            [
                [
                    [
                        [
                            [PageData() for _ in range(PAGE_PER_BLOCK)]
                            for _ in range(BLOCK_PER_PLANE)
                        ]
                        for _ in range(PLANE_PER_DIE)
                    ]
                    for _ in range(DIE_PER_CHIP)
                ]
                for _ in range(CHIP_PER_CHANNEL)
            ]
            for _ in range(CHANNEL_NO)
        ]

        if not QUIET:
            print("PHY initialization complete.")

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self._channel_busy is not None, "PHY _channel_busy is not set"
        assert self._active_transfers is not None, "PHY _active_transfers is not set"
        assert self._pending_transfers is not None, "PHY _pending_transfers is not set"
        assert self._chip_bkes is not None, "PHY _chip_bkes is not set"
        self._construction_valid = True

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_chip_bke(self, chip_id: Tuple[int, int]) -> ChipBKE:
        if chip_id not in self._chip_bkes:
            self._chip_bkes[chip_id] = ChipBKE(chip_id)
        return self._chip_bkes[chip_id]

    def channel_is_busy(self, channel_id: int) -> bool:
        active = self._active_transfers[channel_id]
        if active is not None:
            if active.kind == ChannelTransferKind.COMMAND:
                return True
            return any(
                task.kind in DATA_IN_TRANSFER_KINDS or task.kind == ChannelTransferKind.COMMAND
                for task in self._pending_transfers[channel_id]
            )
        for task in self._pending_transfers[channel_id]:
            if task.kind in DATA_IN_TRANSFER_KINDS or task.kind == ChannelTransferKind.COMMAND:
                return True
        return False

    def configure_onfi_timing(self, timing: OnfiTimingConfig) -> None:
        self.onfi_timing = timing

    # ── Callback registration ────────────────────────────────────────────────

    def connect_channel_idle_signal(self, cb: Callable[[int], None]) -> None:
        self._channel_idle_cbs.append(cb)

    def connect_chip_idle_signal(self, cb: Callable[[Tuple[int, int]], None]) -> None:
        self._chip_idle_cbs.append(cb)

    def connect_transaction_serviced_signal(self, cb: Callable) -> None:
        self._transaction_serviced_cbs.append(cb)
    
    def _broadcast_channel_idle(self, channel_id: int) -> None:
        self._channel_busy[channel_id] = False
        for cb in self._channel_idle_cbs:
            cb(channel_id)
        self.schedule_next_channel_transfer(channel_id)

    def _broadcast_chip_idle(self, chip_id: Tuple[int, int]) -> None:
        for cb in self._chip_idle_cbs:
            cb(chip_id)

    def _broadcast_transaction_serviced(self, tr) -> None:
        for cb in self._transaction_serviced_cbs:
            cb(tr)

    # Channel transfer scheduler

    def _next_transfer_sequence(self) -> int:
        self._transfer_sequence += 1
        return self._transfer_sequence

    def _transfer_plane_count(self, transactions: list) -> int:
        return _valid_plane_count(self._participating_plane_count(transactions))

    def _participating_plane_count(self, transactions: list) -> int:
        planes = {
            tr.address.plane
            for tr in transactions
            if tr.address is not None and tr.address.plane >= 0
        }
        return len(planes) or 1

    def _transaction_payload_bytes(self, tr: Transaction) -> int:
        if tr.type in (TransactionType.MAPPING_READ, TransactionType.MAPPING_WRITE):
            entries = sum(1 for bit in tr.bitmap if bit) if tr.bitmap else LPA_NO_PER_MAPPING_PAGE
            sectors = max(1, (entries + LPA_NO_PER_SECTOR - 1) // LPA_NO_PER_SECTOR)
            return sectors * SECTOR_SIZE_BYTES
        sectors = sum(1 for bit in tr.bitmap if bit) if tr.bitmap else len(tr.payload)
        if sectors <= 0:
            sectors = SECTOR_PER_PAGE
        return sectors * SECTOR_SIZE_BYTES

    def _transfer_payload_bytes(self, transactions: list) -> int:
        return sum(self._transaction_payload_bytes(tr) for tr in transactions)

    @staticmethod
    def _bits_to_bytes(bit_count: int) -> int:
        return (bit_count + 7) // 8

    def _data_in_payload_bytes(self, op_kind: str, transactions: list) -> int:
        if op_kind == "search":
            return self._bits_to_bytes(
                self.wl_per_string * self.search_input_bits_per_wl
            )
        if op_kind == "compute":
            return self._bits_to_bytes(
                len(transactions) * self.compute_input_bits_per_sl
            )
        return self._transfer_payload_bytes(transactions)

    def _data_out_payload_bytes(self, op_kind: str, transactions: list) -> int:
        plane_count = self._participating_plane_count(transactions)
        if op_kind == "search":
            bytes_per_plane = self._bits_to_bytes(
                self.bl_per_plane * self.search_match_bits_per_bl
            )
            return plane_count * bytes_per_plane
        if op_kind == "compute":
            bytes_per_plane = self._bits_to_bytes(
                self.bl_per_plane * self.compute_accumulator_bits
            )
            return plane_count * bytes_per_plane
        return self._transfer_payload_bytes(transactions)

    def _command_transfer_duration(self, op_kind: str, transactions: list) -> int:
        plane_count = self._transfer_plane_count(transactions)
        if op_kind == "read":
            return onfi_read_command_duration(plane_count, self.onfi_timing)
        if op_kind in ("write", "search", "compute"):
            return onfi_program_command_duration(plane_count, self.onfi_timing)
        if op_kind == "erase":
            return onfi_erase_command_duration(plane_count, self.onfi_timing)
        raise ValueError(f"Invalid operation type: {op_kind}")

    def _data_in_transfer_duration(
        self, transactions: list, op_kind: Optional[str] = None
    ) -> int:
        if op_kind is None:
            op_kind = _op_kind(transactions[0].type) if transactions else "write"
        payload_bytes = self._data_in_payload_bytes(op_kind, transactions)
        return onfi_data_in_duration(payload_bytes, self.onfi_timing)

    def _data_out_transfer_duration(
        self, transactions: list, op_kind: Optional[str] = None
    ) -> int:
        if op_kind is None:
            op_kind = _op_kind(transactions[0].type) if transactions else "read"
        payload_bytes = self._data_out_payload_bytes(op_kind, transactions)
        return onfi_data_out_duration(
            payload_bytes,
            self._transfer_plane_count(transactions),
            self.onfi_timing,
        )

    def _classify_data_in_transfer(self, transactions: list) -> ChannelTransferKind:
        if transactions and transactions[0].type == TransactionType.GC_WRITE:
            return ChannelTransferKind.GC_WRITE_DATA_IN
        return ChannelTransferKind.USER_DATA_IN

    def _classify_data_out_transfer(self, cmd_type: str, transactions: list) -> ChannelTransferKind:
        tr_type = transactions[0].type if transactions else None
        if tr_type == TransactionType.MAPPING_READ:
            return ChannelTransferKind.MAPPING_DATA_OUT
        if tr_type == TransactionType.GC_READ:
            return ChannelTransferKind.GC_READ_DATA_OUT
        if cmd_type in ("search", "compute"):
            return ChannelTransferKind.STATIC_RESULT_DATA_OUT
        return ChannelTransferKind.USER_DATA_OUT

    def _command_completion_event_type(self, op_kind: str) -> EventType:
        if op_kind == "read":
            return EventType.PHY_READ_CMD_TRANSFERRED
        if op_kind == "write":
            return EventType.PHY_WRITE_CMD_TRANSFERRED
        if op_kind == "erase":
            return EventType.PHY_ERASE_CMD_TRANSFERRED
        if op_kind == "search":
            return EventType.PHY_SEARCH_CMD_TRANSFERRED
        if op_kind == "compute":
            return EventType.PHY_COMPUTE_CMD_TRANSFERRED
        raise ValueError(f"Invalid operation type: {op_kind}")

    def _data_out_completion_event_type(self, op_kind: str) -> EventType:
        if op_kind == "read":
            return EventType.PHY_READ_DATA_TRANSFERRED
        if op_kind == "search":
            return EventType.PHY_SEARCH_DATA_TRANSFERRED
        if op_kind == "compute":
            return EventType.PHY_COMPUTE_DATA_TRANSFERRED
        raise ValueError(f"Command type {op_kind} does not need data-out transfer")

    def _completion_event_type(self, task: ChannelTransferTask) -> EventType:
        if task.kind == ChannelTransferKind.COMMAND:
            return self._command_completion_event_type(task.op_kind)
        if task.is_data_in:
            return EventType.PHY_DATA_IN_TRANSFERRED
        if task.is_data_out:
            return self._data_out_completion_event_type(task.op_kind)
        raise ValueError(f"Unsupported transfer kind: {task.kind}")

    def _submit_channel_transfer(self, task: ChannelTransferTask, *, defer_start: bool = False) -> ChannelTransferTask:
        task.queue_enter_time = CURRENT_TIME()
        task.sequence = self._next_transfer_sequence()
        channel_id = task.channel_id
        if task.kind == ChannelTransferKind.COMMAND:
            active = self._active_transfers[channel_id]
            if active is not None and active.priority > task.priority:
                self._preempt_active_transfer(active)
        self._pending_transfers[channel_id].append(task)
        if not defer_start:
            self.schedule_next_channel_transfer(channel_id)
        return task

    def schedule_next_channel_transfer(self, channel_id: int) -> bool:
        if self._active_transfers[channel_id] is not None:
            return False
        pending = self._pending_transfers[channel_id]
        if not pending:
            self._channel_busy[channel_id] = False
            return False
        best_index = min(
            range(len(pending)),
            key=lambda idx: (pending[idx].priority, pending[idx].sequence),
        )
        task = pending.pop(best_index)
        self._start_channel_transfer(task)
        return True

    def kick_channel_transfer(self, channel_id: int) -> bool:
        if self._active_transfers[channel_id] is not None:
            return False
        if not self._pending_transfers[channel_id]:
            return False
        return self.schedule_next_channel_transfer(channel_id)

    def kick_all_channel_transfers(self) -> bool:
        progressed = False
        for channel_id in range(CHANNEL_NO):
            if self.kick_channel_transfer(channel_id):
                progressed = True
        return progressed

    def _start_channel_transfer(self, task: ChannelTransferTask) -> None:
        now = CURRENT_TIME()
        queue_enter_time = task.queue_enter_time
        if queue_enter_time is not None and now > queue_enter_time:
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_channel_wait(
                    task.transactions,
                    task.op_kind,
                    task.kind.value,
                    queue_enter_time,
                    now,
                )
        task.queue_enter_time = None
        duration = max(0, task.remaining_duration)
        task.start_time = now
        task.finish_time = now + duration
        self._active_transfers[task.channel_id] = task
        self._channel_busy[task.channel_id] = True
        if task.kind == ChannelTransferKind.COMMAND or task.is_data_in:
            chip_bke = self.get_chip_bke(task.chip_id)
            die_bke = chip_bke.get_die_bke(task.die_id)
            die_bke.expected_finish_time = task.finish_time
            chip_bke.Expected_Finish_Time = task.finish_time
        task.completion_event = Register_event(
            event_type=self._completion_event_type(task),
            target=self,
            param={
                "chip_id": task.chip_id,
                "die_id": task.die_id,
                "transactions": task.transactions,
                "transfer_task": task,
            },
            scheduled_time=task.finish_time,
        )

    def _record_transfer_segment(self, task: ChannelTransferTask, start_time: int, finish_time: int) -> None:
        if finish_time <= start_time:
            return
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is None:
            return
        if task.kind == ChannelTransferKind.COMMAND:
            recorder.note_phy_command_phase(
                task.transactions,
                task.op_kind,
                start_time,
                finish_time,
                finish_time - start_time,
            )
        elif task.is_data_in:
            recorder.note_phy_data_in_phase(task.transactions, task.op_kind, start_time, finish_time)
            recorder.add_energy(task.transactions, P_IF * (finish_time - start_time) * 1e-6)
        elif task.is_data_out:
            recorder.note_phy_data_out_phase(task.transactions, task.op_kind, start_time, finish_time)
            recorder.add_energy(task.transactions, P_IF * (finish_time - start_time) * 1e-6)

    def _finish_active_transfer_from_event(self, event: SimEvent) -> Optional[ChannelTransferTask]:
        task = event.param.get("transfer_task")
        if task is None:
            return None
        active = self._active_transfers[task.channel_id]
        if active is not task or task.completion_event is not event:
            return None
        now = CURRENT_TIME()
        start_time = now if task.start_time is None else task.start_time
        self._record_transfer_segment(task, start_time, now)
        task.remaining_duration = 0
        task.completion_event = None
        self._active_transfers[task.channel_id] = None
        self._channel_busy[task.channel_id] = False
        return task

    def _preempt_active_transfer(self, task: ChannelTransferTask) -> bool:
        now = CURRENT_TIME()
        if task.priority <= CHANNEL_TRANSFER_PRIORITY[ChannelTransferKind.COMMAND]:
            return False
        if task.finish_time is None or task.finish_time <= now:
            return False
        if task.completion_event is not None:
            task.completion_event.ignored = True
        start_time = now if task.start_time is None else task.start_time
        self._record_transfer_segment(task, start_time, now)
        task.remaining_duration = max(0, task.finish_time - now)
        task.start_time = None
        task.finish_time = None
        task.completion_event = None
        self._active_transfers[task.channel_id] = None
        self._channel_busy[task.channel_id] = False
        task.sequence = self._next_transfer_sequence()
        self._pending_transfers[task.channel_id].append(task)
        return True

    def _preempt_active_data_out(self, task: ChannelTransferTask) -> bool:
        return self._preempt_active_transfer(task)

    def _enqueue_data_in_transfer(
        self,
        chip_id: Tuple[int, int],
        die_id: int,
        op_kind: str,
        transactions: list,
        *,
        defer_start: bool = True,
    ) -> ChannelTransferTask:
        payload_bytes = self._data_in_payload_bytes(op_kind, transactions)
        task = ChannelTransferTask(
            kind=self._classify_data_in_transfer(transactions),
            channel_id=chip_id[0],
            chip_id=chip_id,
            die_id=die_id,
            transactions=transactions,
            total_duration=onfi_data_in_duration(payload_bytes, self.onfi_timing),
            op_kind=op_kind,
            payload_bytes=payload_bytes,
        )
        return self._submit_channel_transfer(task, defer_start=defer_start)

    def _enqueue_data_out_transfer(
        self,
        chip_id: Tuple[int, int],
        die_id: int,
        cmd_type: str,
        transactions: list,
        *,
        defer_start: bool = True,
    ) -> ChannelTransferTask:
        payload_bytes = self._data_out_payload_bytes(cmd_type, transactions)
        task = ChannelTransferTask(
            kind=self._classify_data_out_transfer(cmd_type, transactions),
            channel_id=chip_id[0],
            chip_id=chip_id,
            die_id=die_id,
            transactions=transactions,
            total_duration=onfi_data_out_duration(
                payload_bytes,
                self._transfer_plane_count(transactions),
                self.onfi_timing,
            ),
            op_kind=cmd_type,
            payload_bytes=payload_bytes,
        )
        return self._submit_channel_transfer(task, defer_start=defer_start)

    # ── TSU → PHY interface ────────────────────────────────────────────────────

    def send_command_to_chip(
        self,
        chip_id: Tuple[int, int],
        transactions: list,
        suspension_required: bool,
    ) -> None:
        """下发命令到 chip

        1. 若 suspension_required 且 chip 非 IDLE，先执行挂起操作。
        2. 将新命令登记为 Die 的 active_command。
        3. 根据操作类型调度命令传输完成事件并将 channel 置为 BUSY。
        """
        if not transactions:
            raise ValueError("Transactions is empty!")

        chip_bke = self.get_chip_bke(chip_id)
        channel_id = chip_id[0]
        now = CURRENT_TIME()

        tr0 = transactions[0]
        op = _op_kind(tr0.type)
        die_id = tr0.address.die
        die_bke = chip_bke.get_die_bke(die_id)

        # 1. Suspend the currently running operation if requested
        if suspension_required and chip_bke.status != ChipStatus.IDLE:
            had_active_command = die_bke.active_command is not None
            die_bke.prepare_suspend(now)
            if had_active_command:
                chip_bke.No_of_active_dies = max(0, chip_bke.No_of_active_dies - 1)
            chip_bke.HasSuspendedCommands = True

        # 2. Register new command on the die
        die_bke.active_command = ActiveCommandInfo(op, transactions)
        chip_bke.status = ChipStatus.TRANSFER
        chip_bke.No_of_active_dies += 1

        # 3. Submit the command/address transfer to the channel scheduler.
        command_duration = self._command_transfer_duration(op, transactions)
        finish_time = now + command_duration
        die_bke.expected_finish_time = finish_time
        chip_bke.Expected_Finish_Time = finish_time
        task = ChannelTransferTask(
            kind=ChannelTransferKind.COMMAND,
            channel_id=channel_id,
            chip_id=chip_id,
            die_id=die_id,
            transactions=transactions,
            total_duration=command_duration,
            op_kind=op,
        )
        self._submit_channel_transfer(task)

    # ── sim_object event handler ───────────────────────────────────────────────

    def execute(self, event: SimEvent) -> None:
        """处理 PHY 仿真事件，对标 Execute_simulator_event()。"""
        if event.ignored:
            return
        from .common import log_execute_event
        log_execute_event(self.__class__.__name__, event)
        ev_type = event.type
        chip_id = event.param.get("chip_id", (-1, -1))
        die_id = event.param.get("die_id", -1)
        transactions = event.param.get("transactions", [])
        transfer_task = event.param.get("transfer_task")
        now = CURRENT_TIME()    

        # ── Command/address transfer phase complete ────────────────────────────

        if ev_type == EventType.PHY_READ_CMD_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            # Cmd+addr sent to chip; chip begins internal read; release channel.
            chip_bke.status = ChipStatus.READ
            finish = now + T_READ_LSB
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "read", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_READ_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_WRITE_CMD_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            if task is not None and task.kind == ChannelTransferKind.COMMAND:
                self._enqueue_data_in_transfer(chip_id, die_id, "write", transactions, defer_start=True)
                self._broadcast_channel_idle(chip_id[0])
                return
            # Cmd+data sent to chip; chip begins internal program; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            is_gc = "gc" in transactions[0].type.value.lower()
            chip_bke.status = ChipStatus.GC_WRITE if is_gc else ChipStatus.WRITE
            finish = now + T_PROG
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "write", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            exec_event = Register_event(
                event_type=EventType.PHY_CHIP_WRITE_COMPLETE,
                target=self,
                param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions},
                scheduled_time=finish,
            )
            for tr in transactions:
                tr.exec_event = exec_event
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_ERASE_CMD_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            # Cmd sent to chip; chip begins internal erase; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.ERASE
            finish = now + T_BERS
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "erase", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            exec_event = Register_event(
                event_type=EventType.PHY_CHIP_ERASE_COMPLETE,
                target=self,
                param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions},
                scheduled_time=finish,
            )
            for tr in transactions:
                tr.exec_event = exec_event
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_SEARCH_CMD_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            if task is not None and task.kind == ChannelTransferKind.COMMAND:
                self._enqueue_data_in_transfer(chip_id, die_id, "search", transactions, defer_start=True)
                self._broadcast_channel_idle(chip_id[0])
                return
            # Cmd sent to chip; chip begins internal search; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.SEARCH
            finish = now + T_SEARCH
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "search", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_SEARCH_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_COMPUTE_CMD_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            if task is not None and task.kind == ChannelTransferKind.COMMAND:
                self._enqueue_data_in_transfer(chip_id, die_id, "compute", transactions, defer_start=True)
                self._broadcast_channel_idle(chip_id[0])
                return
            # Cmd sent to chip; chip begins internal compute; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.COMPUTE
            finish = now + T_COMPUTE
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "compute", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_COMPUTE_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        # ── Chip-internal operation complete ──────────────────────────────────

        elif ev_type == EventType.PHY_DATA_IN_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if task is None:
                return
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            op_kind = task.op_kind
            if op_kind == "write":
                is_gc = "gc" in transactions[0].type.value.lower()
                chip_bke.status = ChipStatus.GC_WRITE if is_gc else ChipStatus.WRITE
                finish = now + T_PROG
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    recorder.note_phy_array_phase(transactions, "write", now, finish)
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                exec_event = Register_event(
                    event_type=EventType.PHY_CHIP_WRITE_COMPLETE,
                    target=self,
                    param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions},
                    scheduled_time=finish,
                )
                for tr in transactions:
                    tr.exec_event = exec_event
            elif op_kind == "search":
                chip_bke.status = ChipStatus.SEARCH
                finish = now + T_SEARCH
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    recorder.note_phy_array_phase(transactions, "search", now, finish)
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                Register_event(event_type=EventType.PHY_CHIP_SEARCH_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            elif op_kind == "compute":
                chip_bke.status = ChipStatus.COMPUTE
                finish = now + T_COMPUTE
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    recorder.note_phy_array_phase(transactions, "compute", now, finish)
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                Register_event(event_type=EventType.PHY_CHIP_COMPUTE_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            else:
                raise ValueError(f"Invalid data-in operation type: {op_kind}")
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_CHIP_READ_COMPLETE:
            rec = REQUEST_LATENCY_RECORDER()
            if rec is not None:
                rec.add_energy(transactions, P_ARRAY * T_READ_LSB * 1e-6)
            self._handle_array_execution_finished(chip_id, die_id, "read", transactions)

        elif ev_type == EventType.PHY_CHIP_WRITE_COMPLETE:
            rec = REQUEST_LATENCY_RECORDER()
            if rec is not None:
                rec.add_energy(transactions, P_ARRAY * T_PROG * 1e-6)
            self._handle_array_execution_finished(chip_id, die_id, "write", transactions)

        elif ev_type == EventType.PHY_CHIP_ERASE_COMPLETE:
            rec = REQUEST_LATENCY_RECORDER()
            if rec is not None:
                rec.add_energy(transactions, P_ARRAY * T_BERS * 1e-6)
            self._handle_array_execution_finished(chip_id, die_id, "erase", transactions)
        elif ev_type == EventType.PHY_CHIP_SEARCH_COMPLETE:
            rec = REQUEST_LATENCY_RECORDER()
            if rec is not None:
                rec.add_energy(transactions, P_SEARCH_ARRAY * T_SEARCH * 1e-6)
            self._handle_array_execution_finished(chip_id, die_id, "search", transactions)
        elif ev_type == EventType.PHY_CHIP_COMPUTE_COMPLETE:
            rec = REQUEST_LATENCY_RECORDER()
            if rec is not None:
                rec.add_energy(transactions, P_COMPUTE_ARRAY * T_COMPUTE * 1e-6)
            self._handle_array_execution_finished(chip_id, die_id, "compute", transactions)

        # ── Read data-out phase complete ──────────────────────────────────────

        elif ev_type == EventType.PHY_READ_DATA_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.No_of_active_dies = max(0, chip_bke.No_of_active_dies - 1)
            chip_bke._has_data_waiting = False
            die_bke.active_command = None
            # Data DMA'd from chip to controller; complete all waiting read transactions.
            for tr in transactions:
                tr.completed = True
                try:
                    tr.response = self._read_from_storage(tr)
                except RequestFailure as exc:
                    tr.failed = True
                    tr.error_message = str(exc)
                    tr.response = None
                    if not QUIET:
                        debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED failed tr: {tr}")
                    for required_by_tr in tr.required_by_transactions:
                        if tr in required_by_tr.rely_on_transactions:
                            required_by_tr.rely_on_transactions.remove(tr)
                        if required_by_tr.error_message is None:
                            required_by_tr.error_message = tr.error_message
                        required_by_tr.failed = True
                    self._broadcast_transaction_serviced(tr)
                    continue
                if not QUIET:
                    debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, tr: {tr}")
                for required_by_tr in tr.required_by_transactions:
                    if tr in required_by_tr.rely_on_transactions:
                        required_by_tr.rely_on_transactions.remove(tr)
                    required_by_tr.get_response_from_transaction(tr)
                    if not QUIET:
                        debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, required_by_tr: {required_by_tr}")
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, sending resume command")
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies <= 0:
                    chip_bke.No_of_active_dies = 0
                    chip_bke.status = ChipStatus.IDLE
                self._broadcast_channel_idle(channel_id)
        
        elif ev_type == EventType.PHY_SEARCH_DATA_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke._has_data_waiting = False
            die_bke.active_command = None
            chip_bke.No_of_active_dies = max(0, chip_bke.No_of_active_dies - 1)
            for tr in transactions:
                tr.completed = True
                for required_by_tr in tr.required_by_transactions:
                    if tr in required_by_tr.rely_on_transactions:
                        required_by_tr.rely_on_transactions.remove(tr)
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies <= 0:
                    chip_bke.No_of_active_dies = 0
                    chip_bke.status = ChipStatus.IDLE
                self._broadcast_channel_idle(channel_id)
        
        elif ev_type == EventType.PHY_COMPUTE_DATA_TRANSFERRED:
            task = self._finish_active_transfer_from_event(event)
            if transfer_task is not None and task is None:
                return
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke._has_data_waiting = False
            die_bke.active_command = None
            chip_bke.No_of_active_dies = max(0, chip_bke.No_of_active_dies - 1)
            for tr in transactions:
                tr.completed = True
                for required_by_tr in tr.required_by_transactions:
                    if tr in required_by_tr.rely_on_transactions:
                        required_by_tr.rely_on_transactions.remove(tr)
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies <= 0:
                    chip_bke.No_of_active_dies = 0
                    chip_bke.status = ChipStatus.IDLE
                self._broadcast_channel_idle(channel_id)
        else:
            raise ValueError(f"Invalid event type for phy execution: {ev_type}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _handle_array_execution_finished(
        self,
        chip_id: Tuple[int, int],
        die_id: int,
        cmd_type: str,
        transactions: list[Transaction]
    ) -> None:
        """Chip 内部操作完成后的处理，对标 handle_ready_signal_from_chip()。"""
        chip_bke = self.get_chip_bke(chip_id)
        die_bke = chip_bke.get_die_bke(die_id)
        channel_id = chip_id[0]

        if cmd_type in ("read", "search", "compute"):
            # Queue data-out and let PHY/TSU channel-idle arbitration pick the next transfer.
            if die_bke.active_command:
                chip_bke._has_data_waiting = True
            self._enqueue_data_out_transfer(chip_id, die_id, cmd_type, transactions, defer_start=True)
            if self._active_transfers[channel_id] is None:
                self._broadcast_channel_idle(channel_id)
            # If channel busy, _broadcast_channel_idle will trigger _transfer_data
            # via TSU callback → try_activate → (no more queued work) → the waiting
            # data-out will be picked up when channel next becomes idle.
            # For simplicity, we handle it here: if channel became idle after our
            # check, the data will stay in _waiting_data_out until the next idle event.

        elif cmd_type == "write" or cmd_type == "erase":  # write or erase
            active_matches = (
                die_bke.active_command is not None
                and die_bke.active_command.transactions is transactions
            )
            for tr in transactions:
                if tr.completed:
                    continue
                tr.completed = True
                if tr.type in (
                    TransactionType.MAPPING_WRITE,
                    TransactionType.USER_WRITE,
                    TransactionType.USER_STATIC_WRITE,
                    TransactionType.GC_WRITE,
                ):
                    self._write_to_storage(tr)
                if not QUIET:
                    debug_info(f"[PHY] following tr completed: {tr}")
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    recorder.note_persistence_completed(tr, CURRENT_TIME())
                for required_by_tr in tr.required_by_transactions:
                    if (
                        required_by_tr.type == TransactionType.MAPPING_WRITE
                        and tr.type == TransactionType.MAPPING_WRITE
                    ):
                        required_by_tr.get_response_from_transaction(tr)
                    if tr in required_by_tr.rely_on_transactions:
                        required_by_tr.rely_on_transactions.remove(tr) # remove reliance
                    if not QUIET:
                        debug_info(f"[PHY] removed reliance by {required_by_tr}")
                self._broadcast_transaction_serviced(tr)
            if active_matches:
                die_bke.active_command = None
            chip_bke.No_of_active_dies = max(0, chip_bke.No_of_active_dies - 1)

            if chip_bke.No_of_active_dies == 0:
                if chip_bke.HasSuspendedCommands:
                    debug_info(f"[PHY] _handle_array_execution_finished, sending resume command")
                    self._send_resume_command(chip_id)
                else:
                    debug_info(f"[PHY] _handle_array_execution_finished, chip {chip_id} is idle")
                    chip_bke.status = ChipStatus.IDLE
                    self._broadcast_chip_idle(chip_id)
        else:
            raise ValueError(f"[PHY] _handle_array_execution_finished: Invalid command type: {cmd_type}")

    def _transfer_data(
        self,
        chip_id: Tuple[int, int],
        die_id: int,
        cmd_type: str,
        transactions: list[Transaction]
    ) -> None:
        """启动读数据回传阶段"""
        self._enqueue_data_out_transfer(chip_id, die_id, cmd_type, transactions, defer_start=False)

    def _send_resume_command(self, chip_id: Tuple[int, int]) -> None:
        """恢复被挂起的命令"""
        chip_bke = self.get_chip_bke(chip_id)
        now = CURRENT_TIME()

        for die_bke in chip_bke.dies.values():
            if die_bke.suspended_command is None:
                continue
            remaining = die_bke.prepare_resume()
            chip_bke.No_of_active_dies += 1
            if remaining <= 0:
                # Same-time event ordering can suspend an operation whose finish
                # event is already due. Complete it just after resume instead of
                # aborting the whole replay.
                remaining = 1
            cmd_type = die_bke.active_command.cmd_type
            transactions = die_bke.active_command.transactions

            if cmd_type == "read":
                chip_bke.status = ChipStatus.READ
                finish = now + remaining
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                Register_event(
                    EventType.PHY_CHIP_READ_COMPLETE,
                    self,
                    {
                        "chip_id": chip_id,
                        "die_id": die_bke.die_id,
                        "transactions": transactions,
                    },
                    finish,
                )
            elif cmd_type == "write":
                is_gc = "gc" in transactions[0].type.value.lower()
                chip_bke.status = ChipStatus.GC_WRITE if is_gc else ChipStatus.WRITE
                finish = now + remaining
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                exec_event = Register_event(
                    EventType.PHY_CHIP_WRITE_COMPLETE,
                    self,
                    {
                        "chip_id": chip_id,
                        "die_id": die_bke.die_id,
                        "transactions": transactions,
                    },
                    finish,
                )
                for tr in transactions:
                    tr.exec_event = exec_event
            elif cmd_type == "erase":
                chip_bke.status = ChipStatus.ERASE
                finish = now + remaining
                die_bke.expected_finish_time = finish
                chip_bke.Expected_Finish_Time = finish
                exec_event = Register_event(
                    EventType.PHY_CHIP_ERASE_COMPLETE,
                    self,
                    {
                        "chip_id": chip_id,
                        "die_id": die_bke.die_id,
                        "transactions": transactions,
                    },
                    finish,
                )
                for tr in transactions:
                    tr.exec_event = exec_event
            else:
                raise ValueError(f"Calling resume for command type {cmd_type} is unreasonable!")

        chip_bke.HasSuspendedCommands = False
    
    # ---------------- PHY storage transaction ----------------------
    def _write_to_storage(self, tr: Transaction) -> None:
        if tr.type == TransactionType.MAPPING_WRITE:
            page_type = PageType.MAPPING
        elif tr.type in (
            TransactionType.USER_WRITE,
            TransactionType.USER_STATIC_WRITE,
            TransactionType.GC_WRITE,
        ):
            page_type = PageType.USER
        else:
            raise ValueError(f"Invalid transaction type for writing to storage: {tr.type}")
        channel_id = tr.address.channel
        chip_id = tr.address.chip
        die_id = tr.address.die
        plane_id = tr.address.plane
        sub_plane_id = tr.address.sub_plane
        page_id = tr.address.page
        pagedata = self._storage[channel_id][chip_id][die_id][plane_id][sub_plane_id][page_id]
        pagedata.function = page_type
        if page_type == PageType.USER:
            pagedata.lpa = tr.lpa
            pagedata.mvpn = INVALID_MVPN
            pagedata.valid_bitmap = [0] * SECTOR_PER_PAGE
            pagedata.data = [INVALID_DATA] * SECTOR_PER_PAGE
            for i in range(SECTOR_PER_PAGE):
                if i < len(tr.bitmap) and tr.bitmap[i] == 1:
                    pagedata.valid_bitmap[i] = 1
                    if i < len(tr.payload):
                        pagedata.data[i] = tr.payload[i]
        else:
            pagedata.lpa = INVALID_LPA
            pagedata.mvpn = tr.mvpn
            pagedata.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
            pagedata.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if i < len(tr.bitmap) and tr.bitmap[i] == 1:
                    pagedata.valid_bitmap[i] = 1
                    if i < len(tr.payload):
                        pagedata.data[i] = tr.payload[i]
        return

    
    def _make_empty_mapping_page(self) -> PageData:
        empty = PageData()
        empty.function = PageType.MAPPING
        empty.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
        empty.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
        empty.mvpn = INVALID_MVPN
        empty.lpa = INVALID_LPA
        return empty

    def _read_from_storage(self, tr: Transaction) -> PageData:
        pagedata = self._storage[tr.address.channel][tr.address.chip][tr.address.die][tr.address.plane][tr.address.sub_plane][tr.address.page]
        # 页面从未被写入：返回全空 MAPPING 页面
        if pagedata.function is None:
            return self._make_empty_mapping_page()
        if pagedata.function == PageType.MAPPING:
            if pagedata.mvpn == INVALID_MVPN or pagedata.lpa != INVALID_LPA:
                if tr.source_req is not None and tr.type == TransactionType.MAPPING_READ:
                    raise RequestFailure("[PHY] <_read_from_storage> accessing invalid mapping page!")
                raise ValueError(f"[PHY] <_read_from_storage> accessing invalid mapping page!")
            valid = True
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if tr.bitmap[i] == 1 and pagedata.valid_bitmap[i] == 0:
                    valid = False
                    break
            if not valid and tr.type not in [TransactionType.GC_READ]:
                if tr.source_req is not None and tr.type == TransactionType.MAPPING_READ:
                    raise RequestFailure("[PHY] <_read_from_storage> accessing invalid lpa in mapping page!")
                # 内部操作（无 source_req）：不崩溃，返回空数据让上层处理
                return self._make_empty_mapping_page()
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if tr.bitmap[i] == 1 and pagedata.data[i] == INVALID_PPA:
                    if tr.source_req is not None and tr.type == TransactionType.MAPPING_READ:
                        raise RequestFailure("[PHY] <_read_from_storage> accessing invalid ppa in mapping page!")
                    return self._make_empty_mapping_page()
        elif pagedata.function == PageType.USER:
            if pagedata.lpa == INVALID_LPA or pagedata.mvpn != INVALID_MVPN:
                if tr.source_req is not None and tr.type == TransactionType.USER_READ:
                    raise RequestFailure("[PHY] <_read_from_storage> accessing invalid user page!")
                raise ValueError(f"[PHY] <_read_from_storage> accessing invalid user page!")
            valid = True
            for i in range(SECTOR_PER_PAGE):
                if tr.type not in [TransactionType.GC_READ] and tr.bitmap[i] == 1 and pagedata.data[i] == INVALID_DATA:
                    valid = False
                    break
            if not valid:
                if tr.source_req is not None and tr.type == TransactionType.USER_READ:
                    raise RequestFailure("[PHY] <_read_from_storage> accessing invalid sector in user page!")
                raise ValueError(f"[PHY] <_read_from_storage> accessing invalid sector in user page!")
        return pagedata

    def clear_block_pages(self, addr: FlashAddress) -> None:
        """Erase simulation: reset all PageData in one physical block."""
        ch, chip, die, pl, blk = addr.channel, addr.chip, addr.die, addr.plane, addr.sub_plane
        for pg in range(PAGE_PER_BLOCK):
            self._storage[ch][chip][die][pl][blk][pg] = PageData()


# ── Module-level utility ──────────────────────────────────────────────────────

def tr0_type(die_bke: DieBKE) -> str:
    """Safely extract the type string of the first transaction in active_command."""
    if die_bke.active_command and die_bke.active_command.transactions:
        return die_bke.active_command.transactions[0].type
    return ""
