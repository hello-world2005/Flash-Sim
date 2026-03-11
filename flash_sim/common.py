# -*- coding: utf-8 -*-
"""公共定义：sim_object、事件类型常量、Request、Event。"""

from dataclasses import dataclass, field
from typing import Any, List, Optional
from enum import Enum
from .config import TimingConfig


# ----- 基类 -----
class sim_object:
    """仿真对象基类，Host/PCIe_link/HIL 等继承，统一 execute(event) 接口。"""
    def execute(self, event: "SimEvent") -> None:
        """事件处理入口，子类按 event.type 分发。"""
        raise NotImplementedError


# ----- 事件类型常量 -----
REQ_INIT = "REQ_INIT"
DELIVER = "DELIVER"

# PCIe message 类型（Host/Device 间）
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

# 请求类型（req.type，用于 FTL/HIL）
READ = "READ"
WRITE = "WRITE"
SEARCH = "SEARCH"
COMPUTE = "COMPUTE"
MAPPING = "MAPPING"

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

# ----- Die Status ----------
class DieStatus(Enum):
    READ = 1
    WRITE = 2
    SEARCH = 3
    COMPUTE = 4
    IDLE = 0

# ----- Chip Status --------
class ChipStatus(Enum):
    IDLE = 0
    READ = 1
    WRITE = 2
    SEARCH = 3
    COMPUTE = 4
    ERASE = 5
    GC_WRITE = 6



# ----- Request 数据类 -----
@dataclass
class Request:
    """Host 下发的 IO 请求，供 Host、HIL、FTL 使用。"""
    type: str  # READ, WRITE, SEARCH, COMPUTE, MAPPING
    sq_id: Optional[int] = None
    transaction_list: List[Any] = field(default_factory=list)
    serviced_trans: int = 0
    lha_start: int = 0   # start logical sector address
    size: int = 0   # size of request in sectors
    payload: Any = None

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
    type: str
    target: Any
    param: Any


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

@dataclass
class Transaction:
    source_req: Request
    type: str
    lpa: int = 0
    mvpn: int = 0
    sector_bitmap: list[int] = field(default_factory=lambda: [0] * SECTOR_PER_PAGE) # 0: not accessed, 1: accessed
    address: tuple = field(default_factory=lambda: (0, 0, 0, 0, 0, 0))
    related_transactions = []
    completed: bool = False

class Transaction_WR(Transaction):
    def __init__(self, source_req: Request, lpa: int, mvpn: int, sector_bitmap: list[int], data: bytes):
        super().__init__(source_req, lpa, mvpn, sector_bitmap)
        self.data = data

class Transaction_RD(Transaction):
    def __init__(self, source_req: Request, lpa: int, mvpn: int, sector_bitmap: list[int], address: FlashAddress):
        super().__init__(source_req, lpa, mvpn, sector_bitmap, address)
        self.type = "read"

class Transaction_SEARCH(Transaction):
    def __init__(self, source_req: Request, lpa: int, mvpn: int, sector_bitmap: list[int]):
        super().__init__(source_req, lpa, mvpn, sector_bitmap)
        
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