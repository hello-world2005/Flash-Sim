# -*- coding: utf-8 -*-
"""公共定义：sim_object、事件类型常量、Request、Event。"""

from dataclasses import dataclass, field
import time
from typing import Any, List, Optional
from enum import Enum
from .config import TimingConfig
from __future__ import annotations

class EventType(Enum):
    # ----- 事件类型常量 -----
    REQ_INIT = "REQ_INIT"
    DELIVER = "DELIVER"
    # ── PHY event type constants ─────────────────────────────────────────────────
    PHY_READ_CMD_TRANSFERRED  = "PHY_READ_CMD_TRANSFERRED"   # cmd/addr sent → chip reads
    PHY_WRITE_CMD_TRANSFERRED = "PHY_WRITE_CMD_TRANSFERRED"  # cmd+data sent → chip programs
    PHY_ERASE_CMD_TRANSFERRED = "PHY_ERASE_CMD_TRANSFERRED"  # cmd sent → chip erases
    PHY_SEARCH_CMD_TRANSFERRED = "PHY_SEARCH_CMD_TRANSFERRED"  # cmd sent → chip searches
    PHY_COMPUTE_CMD_TRANSFERRED = "PHY_COMPUTE_CMD_TRANSFERRED"  # cmd sent → chip computes

    PHY_CHIP_READ_COMPLETE    = "PHY_CHIP_READ_COMPLETE"     # chip internal read done
    PHY_CHIP_WRITE_COMPLETE   = "PHY_CHIP_WRITE_COMPLETE"    # chip internal program done
    PHY_CHIP_ERASE_COMPLETE   = "PHY_CHIP_ERASE_COMPLETE"   # chip internal erase done
    PHY_CHIP_SEARCH_COMPLETE   = "PHY_CHIP_SEARCH_COMPLETE"   # chip internal search done
    PHY_CHIP_COMPUTE_COMPLETE   = "PHY_CHIP_COMPUTE_COMPLETE"   # chip internal compute done

    PHY_READ_DATA_TRANSFERRED = "PHY_READ_DATA_TRANSFERRED"  # read data back to controller
    PHY_SEARCH_DATA_TRANSFERRED = "PHY_SEARCH_DATA_TRANSFERRED"  # search data back to controller
    PHY_COMPUTE_DATA_TRANSFERRED = "PHY_COMPUTE_DATA_TRANSFERRED"  # compute data back to controller

class MessageType(Enum):
    # Host send, Device excute
    WRITE_REQ = "WRITE_REQ"
    READ_REQ = "READ_REQ"
    SEARCH_REQ = "SEARCH_REQ"
    COMPUTE_REQ = "COMPUTE_REQ"

    WRITE_DATA = "WRITE_DATA"
    SEARCH_DATA = "SEARCH_DATA"
    COMPUTE_DATA = "COMPUTE_DATA"
    # Device send, Host excute
    WRITE_DATA_REQ = "WRITE_DATA_REQ"
    SEARCH_DATA_REQ = "SEARCH_DATA_REQ"
    COMPUTE_DATA_REQ = "COMPUTE_DATA_REQ"

    WRITE_DATA_RECEIVED = "WRITE_DATA_RECEIVED"
    SEARCH_DATA_RECEIVED = "SEARCH_DATA_RECEIVED"
    COMPUTE_DATA_RECEIVED = "COMPUTE_DATA_RECEIVED"
    READ_REQ_RECEIVED = "READ_REQ_RECEIVED"

    READ_RES_SEND_BACK = "READ_RES_SEND_BACK"
    SEARCH_RES_SEND_BACK = "SEARCH_RES_SEND_BACK"
    COMPUTE_RES_SEND_BACK = "COMPUTE_RES_SEND_BACK"

    REQ_COMP = "REQ_COMP"


# ???
MAPPING = "MAPPING"

class RequestType(Enum):
    # 请求类型（req.type，用于 FTL/HIL）
    READ = "READ"
    WRITE = "WRITE"
    SEARCH = "SEARCH"
    COMPUTE = "COMPUTE"

class TransactionType(Enum):
    MAPPING_READ = "MAPPING_READ"
    MAPPING_WRITE = "MAPPING_WRITE"
    USER_READ = "USER_READ"
    USER_WRITE = "USER_WRITE"
    USER_SEARCH = "USER_SEARCH"
    USER_COMPUTE = "USER_COMPUTE"
    GC_WRITE = "GC_WRITE"
    GC_ERASE = "GC_ERASE"
    GC_READ = "GC_READ"

# Host Memory config
CQ_ENTRY_SIZE_BASIC = 128
SQ_ENTRY_SIZE = 128

# 硬件配置
CHANNEL_NO = 8
CHIP_PER_CHANNEL = 4
DIE_PER_CHIP = 1
PLANE_PER_DIE = 4
BLOCK_PER_PLANE = 2048
LAYER_PER_BLOCK = 256
SL_PER_BLOCK = 2
SSL_PER_SL = 4
PAGE_PER_BLOCK = LAYER_PER_BLOCK * SL_PER_BLOCK * SSL_PER_SL
SECTOR_PER_PAGE = 64

COMPUTE_MAX_PARALLEL_SL = 256
SEARCH_MAX_PARALLEL_WL = 256
PAGE_NO_PER_SEARCH_BANK = SEARCH_MAX_PARALLEL_WL
PAGE_NO_PER_COMPUTE_BANK = COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL
COMPUTE_BANK_PER_PLANE = BLOCK_PER_PLANE * SL_PER_BLOCK // COMPUTE_MAX_PARALLEL_SL
SEARCH_BANK_PER_PLANE = SSL_PER_SL * SL_PER_BLOCK * BLOCK_PER_PLANE

STATIC_CHIP_PER_CHANNEL = 1
TOT_RANDOM_SECTOR_NO = SECTOR_PER_PAGE * PAGE_PER_BLOCK * BLOCK_PER_PLANE * PLANE_PER_DIE \
    * DIE_PER_CHIP * CHANNEL_NO * (CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL)


# ----- 常量 -----
CMT_SIZE = 4096
LPA_NO_PER_MAPPING_PAGE = 512
NUM_OF_QUEUES = 8
VIRTUAL_DATA_ADDRESS = 0xFFFFFFFFFFFFFFFF

# ----- Die Status ----------
class DieStatus(Enum):
    READ = "READ"
    WRITE = "WRITE"
    SEARCH = "SEARCH"
    COMPUTE = "COMPUTE"
    IDLE = "IDLE"

# ----- Chip Status --------
class ChipStatus(Enum):
    IDLE = "IDLE"
    READ = "READ"
    WRITE = "WRITE"
    SEARCH = "SEARCH"
    COMPUTE = "COMPUTE"
    ERASE = "ERASE"
    GC_WRITE = "GC_WRITE"

@dataclass
class Transaction:
    source_req: Request
    type: TransactionType
    lpa: int = 0
    address: FlashAddress = field(default_factory=lambda: FlashAddress(channel=-1, chip=-1, die=-1, plane=-1, sub_plane=-1, page=-1))
    bitmap: list[int] = field(default_factory=list)
    related_transactions: list['Transaction'] = field(default_factory=list)
    completed: bool = False
    exec_event: Optional[SimEvent] = None

# ----- Request 数据类 -----
@dataclass
class Request:
    """Host 下发的 IO 请求，供 Host、HIL、FTL 使用。"""
    type: RequestType  # READ, WRITE, SEARCH, COMPUTE, MAPPING
    sq_id: Optional[int] = None
    transaction_list: List[Transaction] = field(default_factory=list)
    serviced_trans: int = 0
    lha_start: int = 0   # start logical sector address
    size: int = 0   # size of request
    data_address: Optional[int] = None
    data_size: Optional[int] = None

    def is_serviced(self) -> bool:
        """是否所有 transaction 已处理完成。"""
        if not self.transaction_list:
            return True
        for tr in self.transaction_list:
            if not tr.completed:
                return False
        return True


@dataclass
class FlashAddress:
    channel: int
    chip: int
    die: int
    plane: int
    sub_plane: int
    page: int

# ----- Event 视图（供 execute(event) 使用） -----
@dataclass
class SimEvent:
    """仿真事件：type, target, param。"""
    type: EventType
    target: Any
    time: int
    param: Optional[Any] = None
    ignored: bool = False


@dataclass
class cmt_entry:
    ppa: int
    dirty: bool

class GTDEntry:
    def __init__(self, address) -> None:
        self.address = address
        self.valid_bitmap = [0 for _ in LPA_NO_PER_MAPPING_PAGE]

    def set_valid_bitmap(self, lpa, value):
        self.valid_bitmap[lpa%LPA_NO_PER_MAPPING_PAGE] = value
        
# ── Simulation time / event scheduling (set by Engine at startup) ──────────
_time_provider = None       # () -> int   returns current sim time in ns
_event_scheduler = None     # (event_type, target, param, scheduled_time) -> None


def CURRENT_TIME() -> int:
    """Return current simulation time in nanoseconds."""
    if _time_provider is not None:
        return _time_provider()
    raise ValueError("Time provider is not initialized")


def Register_event(event_type: str, target: Any, param: Any, scheduled_time: int) -> None:
    """Register a future simulation event."""
    if _event_scheduler is not None:
        _event_scheduler(event_type, target, param, scheduled_time)
    else:
        raise ValueError("Event scheduler is not initialized")


# ── Flash timing constants (nanoseconds) ────────────────────────────────────
PHY_CMD_ADDR_TIME = 100          # command + address bus transfer time
PHY_DATA_IN_TIME  = 5_000        # data transfer from controller to chip (write)
PHY_DATA_OUT_TIME = 5_000        # data transfer from chip to controller (read)
T_READ_LSB        = TimingConfig.t_r_lsb       # chip internal LSB read latency (tR)
T_PROG            = TimingConfig.t_prog_lsb    # chip internal program latency (tPROG)
T_BERS            = TimingConfig.t_bers   # chip internal erase latency (tBERS)
T_SEARCH          = TimingConfig.t_search_lsb    # chip internal search latency (tSEARCH)
T_COMPUTE         = TimingConfig.t_compute_lsb   # chip internal compute latency (tCOMPUTE)

# ── Suspension thresholds (ns) ───────────────────────────────────────────────
REASONABLE_TIME_SUSPEND_WRITE_FOR_READ  = 100_000
REASONABLE_TIME_SUSPEND_ERASE_FOR_READ  = 1_000_000
REASONABLE_TIME_SUSPEND_ERASE_FOR_WRITE = 1_000_000

