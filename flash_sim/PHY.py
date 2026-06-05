# -*- coding: utf-8 -*-
"""Flash 物理层：芯片/Die 状态管理与事件驱动命令执行。

SEARCH/COMPUTE 专属操作由子类或独立路径扩展，此处仅实现 READ/WRITE/ERASE。
"""

from doctest import debug
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


# ── Data structures ───────────────────────────────────────────────────────────

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
    

# ── PHY class ─────────────────────────────────────────────────────────────────

class PHY():
    """Flash 物理层。接收 TSU 下发的命令（send_command_to_chip），通过仿真事件驱动 chip/channel 状态机，事务完成时通过回调通知 TSU 及上层。
    """
    def __init__(self):
        print("Initializing PHY...")
        self._construction_valid: bool = False
        self._channel_busy: List[bool] = [False] * CHANNEL_NO
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

        print("PHY initialization complete.")

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self._channel_busy is not None, "PHY _channel_busy is not set"
        assert self._chip_bkes is not None, "PHY _chip_bkes is not set"
        self._construction_valid = True

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_chip_bke(self, chip_id: Tuple[int, int]) -> ChipBKE:
        if chip_id not in self._chip_bkes:
            self._chip_bkes[chip_id] = ChipBKE(chip_id)
        return self._chip_bkes[chip_id]

    def channel_is_busy(self, channel_id: int) -> bool:
        return self._channel_busy[channel_id]

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

    def _broadcast_chip_idle(self, chip_id: Tuple[int, int]) -> None:
        for cb in self._chip_idle_cbs:
            cb(chip_id)

    def _broadcast_transaction_serviced(self, tr) -> None:
        for cb in self._transaction_serviced_cbs:
            cb(tr)

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
            die_bke.prepare_suspend(now)
            chip_bke.HasSuspendedCommands = True

        # 2. Register new command on the die
        die_bke.active_command = ActiveCommandInfo(op, transactions)
        chip_bke.No_of_active_dies += 1

        # 3. Schedule the appropriate command-transfer event
        if op == "read":
            finish_time = now + PHY_CMD_ADDR_TIME
            ev = EventType.PHY_READ_CMD_TRANSFERRED
        elif op == "write":
            finish_time = now + PHY_CMD_ADDR_TIME + PHY_DATA_IN_TIME
            ev = EventType.PHY_WRITE_CMD_TRANSFERRED
        elif op == "erase":
            finish_time = now + PHY_CMD_ADDR_TIME
            ev = EventType.PHY_ERASE_CMD_TRANSFERRED
        elif op == "search":
            finish_time = now + PHY_CMD_ADDR_TIME + PHY_DATA_IN_TIME
            ev = EventType.PHY_SEARCH_CMD_TRANSFERRED
        elif op == "compute":
            finish_time = now + PHY_CMD_ADDR_TIME + PHY_DATA_IN_TIME
            ev = EventType.PHY_COMPUTE_CMD_TRANSFERRED
        else:
            raise ValueError(f"Invalid operation type: {op}")

        die_bke.expected_finish_time = finish_time
        chip_bke.Expected_Finish_Time = finish_time
        self._channel_busy[channel_id] = True
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_phy_command_phase(
                transactions,
                op,
                now,
                finish_time,
                PHY_CMD_ADDR_TIME,
            )
        # energy: data-in phase, attributed per-request
        if recorder is not None:
            data_in_ns = finish_time - (now + PHY_CMD_ADDR_TIME)
            if data_in_ns > 0 and op in ("write", "search", "compute"):
                recorder.add_energy(transactions, P_IF * data_in_ns * 1e-6)
        Register_event(event_type=ev, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish_time)

    # ── sim_object event handler ───────────────────────────────────────────────

    def execute(self, event: SimEvent) -> None:
        """处理 PHY 仿真事件，对标 Execute_simulator_event()。"""
        from .common import log_execute_event
        log_execute_event(self.__class__.__name__, event)
        ev_type = event.type
        chip_id = event.param.get("chip_id", (-1, -1))
        die_id = event.param.get("die_id", -1)
        transactions = event.param.get("transactions", [])
        now = CURRENT_TIME()    

        # ── Command/address transfer phase complete ────────────────────────────

        if ev_type == EventType.PHY_READ_CMD_TRANSFERRED:
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            # Cmd+addr sent to chip; chip begins internal read; release channel.
            chip_bke.status = ChipStatus.READ
            self._channel_busy[channel_id] = False
            finish = now + T_READ_LSB
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "read", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_READ_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_WRITE_CMD_TRANSFERRED:
            # Cmd+data sent to chip; chip begins internal program; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            is_gc = "gc" in transactions[0].type.value.lower()
            chip_bke.status = ChipStatus.GC_WRITE if is_gc else ChipStatus.WRITE
            self._channel_busy[channel_id] = False
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
            # Cmd sent to chip; chip begins internal erase; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.ERASE
            self._channel_busy[channel_id] = False
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
            # Cmd sent to chip; chip begins internal search; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.SEARCH
            self._channel_busy[channel_id] = False
            finish = now + T_SEARCH
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "search", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_SEARCH_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        elif ev_type == EventType.PHY_COMPUTE_CMD_TRANSFERRED:
            # Cmd sent to chip; chip begins internal compute; release channel.
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.status = ChipStatus.COMPUTE
            self._channel_busy[channel_id] = False
            finish = now + T_COMPUTE
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_phy_array_phase(transactions, "compute", now, finish)
            die_bke.expected_finish_time = finish
            chip_bke.Expected_Finish_Time = finish
            Register_event(event_type=EventType.PHY_CHIP_COMPUTE_COMPLETE, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=finish)
            self._broadcast_channel_idle(channel_id)

        # ── Chip-internal operation complete ──────────────────────────────────

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
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke.No_of_active_dies -= 1
            self._channel_busy[channel_id] = False
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
                    debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED failed tr: {tr}")
                    for required_by_tr in tr.required_by_transactions:
                        if tr in required_by_tr.rely_on_transactions:
                            required_by_tr.rely_on_transactions.remove(tr)
                        if required_by_tr.error_message is None:
                            required_by_tr.error_message = tr.error_message
                        required_by_tr.failed = True
                    self._broadcast_transaction_serviced(tr)
                    continue
                debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, tr: {tr}")
                for required_by_tr in tr.required_by_transactions:
                    required_by_tr.rely_on_transactions.remove(tr)
                    required_by_tr.get_response_from_transaction(tr)
                    debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, required_by_tr: {required_by_tr}")
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                debug_info(f"[PHY] PHY_READ_DATA_TRANSFERRED, sending resume command")
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies == 0:
                    chip_bke.status = ChipStatus.IDLE
                self._broadcast_channel_idle(channel_id)
        
        elif ev_type == EventType.PHY_SEARCH_DATA_TRANSFERRED:
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke._has_data_waiting = False
            die_bke.active_command = None
            chip_bke.No_of_active_dies -= 1
            self._channel_busy[channel_id] = False
            for tr in transactions:
                tr.completed = True
                for required_by_tr in tr.required_by_transactions:
                    required_by_tr.rely_on_transactions.remove(tr)
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies == 0:
                    chip_bke.status = ChipStatus.IDLE
                self._broadcast_channel_idle(channel_id)
        
        elif ev_type == EventType.PHY_COMPUTE_DATA_TRANSFERRED:
            chip_bke = self.get_chip_bke(chip_id)
            die_bke = chip_bke.get_die_bke(die_id)
            channel_id = chip_id[0]
            chip_bke._has_data_waiting = False
            die_bke.active_command = None
            chip_bke.No_of_active_dies -= 1
            self._channel_busy[channel_id] = False
            for tr in transactions:
                tr.completed = True
                for required_by_tr in tr.required_by_transactions:
                    required_by_tr.rely_on_transactions.remove(tr)
                self._broadcast_transaction_serviced(tr)
            if chip_bke.HasSuspendedCommands:
                self._send_resume_command(chip_id)
            else:
                if chip_bke.No_of_active_dies == 0:
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
            # Queue transactions for data-out; start DMA if channel is free.
            if die_bke.active_command:
                chip_bke._has_data_waiting = True
            if not self._channel_busy[channel_id]:
                self._transfer_data(chip_id, die_id, cmd_type, transactions)
            # If channel busy, _broadcast_channel_idle will trigger _transfer_data
            # via TSU callback → try_activate → (no more queued work) → the waiting
            # data-out will be picked up when channel next becomes idle.
            # For simplicity, we handle it here: if channel became idle after our
            # check, the data will stay in _waiting_data_out until the next idle event.

        elif cmd_type == "write" or cmd_type == "erase":  # write or erase
            if die_bke.active_command:
                for tr in transactions:
                    tr.completed = True
                    if tr.type in (
                        TransactionType.MAPPING_WRITE,
                        TransactionType.USER_WRITE,
                        TransactionType.USER_STATIC_WRITE,
                        TransactionType.GC_WRITE,
                    ):
                        self._write_to_storage(tr)
                    debug_info(f"[PHY] following tr completed: {tr}")
                    recorder = REQUEST_LATENCY_RECORDER()
                    if recorder is not None:
                        recorder.note_persistence_completed(tr, CURRENT_TIME())
                    for required_by_tr in tr.required_by_transactions:
                        required_by_tr.rely_on_transactions.remove(tr) # remove reliance
                        debug_info(f"[PHY] removed reliance by {required_by_tr}")
                    self._broadcast_transaction_serviced(tr)
            die_bke.active_command = None
            chip_bke.No_of_active_dies -= 1

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
        channel_id = chip_id[0]
        self._channel_busy[channel_id] = True
        if cmd_type == "read":
            ev = EventType.PHY_READ_DATA_TRANSFERRED
        elif cmd_type == "search":
            ev = EventType.PHY_SEARCH_DATA_TRANSFERRED
        elif cmd_type == "compute":
            ev = EventType.PHY_COMPUTE_DATA_TRANSFERRED
        else:
            raise ValueError(f"Command type {cmd_type} do not need to transfer data!")
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_phy_data_out_phase(
                transactions,
                cmd_type,
                CURRENT_TIME(),
                CURRENT_TIME() + PHY_DATA_OUT_TIME,
            )
        # energy: data-out phase, attributed per-request
        if recorder is not None and cmd_type in ("read", "search", "compute"):
            recorder.add_energy(transactions, P_IF * PHY_DATA_OUT_TIME * 1e-6)
        Register_event(event_type=ev, target=self, param={"chip_id": chip_id, "die_id": die_id, "transactions": transactions}, scheduled_time=CURRENT_TIME() + PHY_DATA_OUT_TIME)

    def _send_resume_command(self, chip_id: Tuple[int, int]) -> None:
        """恢复被挂起的命令"""
        chip_bke = self.get_chip_bke(chip_id)
        now = CURRENT_TIME()

        for die_bke in chip_bke.dies.values():
            if die_bke.suspended_command is None:
                continue
            remaining = die_bke.prepare_resume()
            if remaining <= 0:
                raise ValueError("Remaining time is less than 0!")
            cmd_type = die_bke.active_command.cmd_type
            transactions = die_bke.active_command.transactions

            if cmd_type == "write":
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

    
    def _read_from_storage(self, tr: Transaction) -> PageData:
        pagedata = self._storage[tr.address.channel][tr.address.chip][tr.address.die][tr.address.plane][tr.address.sub_plane][tr.address.page]
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
                raise ValueError(f"[PHY] <_read_from_storage> accessing invalid lpa in mapping page!")
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if tr.bitmap[i] == 1 and pagedata.data[i] == INVALID_PPA:
                    if tr.source_req is not None and tr.type == TransactionType.MAPPING_READ:
                        raise RequestFailure("[PHY] <_read_from_storage> accessing invalid ppa in mapping page!")
                    raise ValueError(f"[PHY] <_read_from_storage> accessing invalid ppa in mapping page!")
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
