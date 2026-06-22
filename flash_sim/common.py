# -*- coding: utf-8 -*-
"""公共定义：sim_object、事件类型常量、Request、Event。"""
from __future__ import annotations
from dataclasses import dataclass, field, fields
import time
from typing import Any, List, Optional
from enum import Enum
try:
    from .config import OnfiTimingConfig, TimingConfig, make_event_runtime_geometry
except ImportError:
    from config import OnfiTimingConfig, TimingConfig, make_event_runtime_geometry

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
    PHY_DATA_IN_TRANSFERRED = "PHY_DATA_IN_TRANSFERRED"  # data-in sent to chip

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
    STATIC_WRITE_REQ = "STATIC_WRITE_REQ"

    WRITE_DATA = "WRITE_DATA"
    SEARCH_DATA = "SEARCH_DATA"
    COMPUTE_DATA = "COMPUTE_DATA"
    STATIC_WRITE_DATA = "STATIC_WRITE_DATA"
    # Device send, Host excute
    WRITE_DATA_REQ = "WRITE_DATA_REQ"
    SEARCH_DATA_REQ = "SEARCH_DATA_REQ"
    COMPUTE_DATA_REQ = "COMPUTE_DATA_REQ"
    STATIC_WRITE_DATA_REQ = "STATIC_WRITE_DATA_REQ"

    WRITE_DATA_RECEIVED = "WRITE_DATA_RECEIVED"
    SEARCH_DATA_RECEIVED = "SEARCH_DATA_RECEIVED"
    COMPUTE_DATA_RECEIVED = "COMPUTE_DATA_RECEIVED"
    READ_REQ_RECEIVED = "READ_REQ_RECEIVED"
    STATIC_WRITE_DATA_RECEIVED = "STATIC_WRITE_DATA_RECEIVED"

    READ_RES_SEND_BACK = "READ_RES_SEND_BACK"
    SEARCH_RES_SEND_BACK = "SEARCH_RES_SEND_BACK"
    COMPUTE_RES_SEND_BACK = "COMPUTE_RES_SEND_BACK"
    STATIC_WRITE_RES_SEND_BACK = "STATIC_WRITE_RES_SEND_BACK"

    REQ_COMP = "REQ_COMP"


REQUEST_STATUS_SUCCESS = "SUCCESS"
REQUEST_STATUS_ERROR = "ERROR"


class RequestFailure(Exception):
    """Request-scoped error that should complete a host request as ERROR."""

    pass


# ???
MAPPING = "MAPPING"

class RequestType(Enum):
    # 请求类型（req.type，用于 FTL/HIL）
    READ = "READ"
    WRITE = "WRITE"
    SEARCH = "SEARCH"
    COMPUTE = "COMPUTE"
    STATIC_WRITE = "STATIC_WRITE"

class TransactionType(Enum):
    MAPPING_READ = "MAPPING_READ"
    MAPPING_WRITE = "MAPPING_WRITE"
    USER_READ = "USER_READ"
    USER_WRITE = "USER_WRITE"
    USER_READ_FOR_WRITE = "USER_READ_FOR_WRITE"
    USER_SEARCH = "USER_SEARCH"
    USER_COMPUTE = "USER_COMPUTE"
    USER_STATIC_WRITE = "USER_STATIC_WRITE"
    GC_WRITE = "GC_WRITE"
    GC_ERASE = "GC_ERASE"
    GC_READ = "GC_READ"

# Host Memory config
CQ_ENTRY_SIZE_BASIC = 128
SQ_ENTRY_SIZE = 128

# FTL CMT config
CMT_TYPE = "shared"

# 硬件配置
geometry = make_event_runtime_geometry()
CHANNEL_NO = geometry.channel_no
CHIP_PER_CHANNEL = geometry.chip_per_channel
DIE_PER_CHIP = geometry.dies
PLANE_PER_DIE = geometry.planes_per_die
BLOCK_PER_PLANE = geometry.blocks_per_plane
SL_PER_BLOCK = geometry.sl_per_block
SSL_PER_SL = geometry.ssl_per_sl
PAGE_PER_BLOCK = geometry.pages_per_block
SECTOR_PER_PAGE = 64

COMPUTE_MAX_PARALLEL_SL = 256
SEARCH_MAX_PARALLEL_WL = 256
PAGE_NO_PER_SEARCH_BANK = SEARCH_MAX_PARALLEL_WL
PAGE_NO_PER_COMPUTE_BANK = COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL
COMPUTE_BANK_PER_PLANE = BLOCK_PER_PLANE * SL_PER_BLOCK // COMPUTE_MAX_PARALLEL_SL
SEARCH_BANK_PER_PLANE = SSL_PER_SL * SL_PER_BLOCK * BLOCK_PER_PLANE

STATIC_CHIP_PER_CHANNEL = 1
STATIC_BASE_LHA = SECTOR_PER_PAGE * PAGE_PER_BLOCK * BLOCK_PER_PLANE * PLANE_PER_DIE \
    * DIE_PER_CHIP * CHANNEL_NO * (CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL) # 1610612736


# ----- 常量 -----
CMT_SIZE = 64
LPA_NO_PER_SECTOR = 4
LPA_NO_PER_MAPPING_PAGE = LPA_NO_PER_SECTOR * SECTOR_PER_PAGE # 256
NUM_OF_QUEUES = 8
VIRTUAL_DATA_ADDRESS = 0xFFFFFFFFFFFFFFFF
GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD = 3
INVALID_LPA = -1
INVALID_MVPN = -1
INVALID_DATA = -1
INVALID_PPA = -1
SECTOR_SIZE_BYTES = 64
DATA_CACHE_LINE_SIZE = 64
DATA_CACHE_CAP = 4096

# PCIe timing config
# Units:
# - PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS: bytes transferred per nanosecond
# - PCIE_PACKET_OVERHEAD_BYTES: fixed per-message packaging overhead in bytes
PCIE_INTERFACE_BANDWIDTH_BYTES_PER_NS = 4
PCIE_PACKET_OVERHEAD_BYTES = 400

# debug_info = print
def debug_info(*args, **kwargs):
    pass

# ── Flash timing constants (nanoseconds) ────────────────────────────────────
PHY_CMD_ADDR_TIME = 100          # command + address bus transfer time
PHY_DATA_IN_TIME  = 5_000        # data transfer from controller to chip (write)
PHY_DATA_OUT_TIME = 5_000        # data transfer from chip to controller (read)
DEFAULT_ONFI_TIMING = OnfiTimingConfig()
ONFI_CHANNEL_WIDTH_BYTES = DEFAULT_ONFI_TIMING.channel_width_bytes
T_READ_LSB        = TimingConfig.t_r_lsb       # chip internal LSB read latency (tR)
T_PROG            = TimingConfig.t_prog_lsb    # chip internal program latency (tPROG)
T_BERS            = TimingConfig.t_bers   # chip internal erase latency (tBERS)
T_SEARCH          = TimingConfig.t_search_lsb    # chip internal search latency (tSEARCH)
T_COMPUTE         = TimingConfig.t_compute_lsb   # chip internal compute latency (tCOMPUTE)

# ── Flash power constants (mW) ───────────────────────────────────────────────
# 参考: Micron NAND datasheet (2018+, 64Gb+ MLC/TLC, 1.8V VCC) 和
#       NANDFlashSim (ACM TOS 2016) 的 per-stage 能耗模型
#
# Per-stage 模型:
#   E_read  = P_ARRAY × tR + P_IF × PHY_DATA_OUT_TIME   (CLE/ALE 忽略)
#   E_prog  = P_IF × PHY_DATA_IN_TIME + P_ARRAY × tPROG  (CLE + status check 忽略)
#   E_erase = P_ARRAY × tBERS                              (CLE + status check 忽略)
#
# 不同 page_type 的 tR/tPROG 在 TimingConfig 中区分，energy 自动跟随:
#   LSB: t_r=75μs,  t_prog=750μs
#   CSB: t_r=100μs, t_prog=1000μs
#   MSB: t_r=150μs, t_prog=1500μs
#
# 参考值 (1.8V):
P_ARRAY = 45      # NAND cell array power (mW), 1.8V × 25mA
P_IF    = 18      # Flash I/O interface power (mW), 1.8V × 10mA

# CIM 功耗 (估算值, 待实验校准)
# SEARCH: 多条WL同时激活 → 阵列电流高于普通读
P_SEARCH_ARRAY = 54  # search array power (mW), +20% vs P_ARRAY
# COMPUTE: 多block并行激活 → 阵列电流大幅增加
P_COMPUTE_ARRAY = 72 # compute array power (mW), +60% vs P_ARRAY

# ── Suspension thresholds (ns) ───────────────────────────────────────────────
REASONABLE_TIME_SUSPEND_WRITE_FOR_READ  = 100_000
REASONABLE_TIME_SUSPEND_ERASE_FOR_READ  = 1_000_000
REASONABLE_TIME_SUSPEND_ERASE_FOR_WRITE = 1_000_000

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
    TRANSFER = "TRANSFER"
    READ = "READ"
    WRITE = "WRITE"
    SEARCH = "SEARCH"
    COMPUTE = "COMPUTE"
    ERASE = "ERASE"
    GC_WRITE = "GC_WRITE"

@dataclass
class Transaction:
    source_req: Optional[Request]
    type: TransactionType
    lpa: int = INVALID_LPA # register lpa if type is not TransactionType.MAPPING_..., 
    mvpn: int = INVALID_MVPN # register mvpn if type is TransactionType.MAPPING_...
    address: FlashAddress = field(default_factory=lambda: FlashAddress(channel=-1, chip=-1, die=-1, plane=-1, sub_plane=-1, page=-1))
    bitmap: list[int] = field(default_factory=list) # register lpa bitmap if type is TransactionType.MAPPING_..., else sector bitmap
    rely_on_transactions: list['Transaction'] = field(default_factory=list)
    required_by_transactions: list['Transaction'] = field(default_factory=list)
    completed: bool = False
    data_ready: bool = True
    exec_event: Optional[SimEvent] = None
    payload: list[int] = field(default_factory=list) # register data if type is TransactionType.USER_..., else payload for mapping write
    response: Optional['PageData'] = None # register response data when necessary
    failed: bool = False
    error_message: Optional[str] = None
    # GC: source physical page before migrate (for mapping / BKE invalidation)
    gc_old_address: Optional[FlashAddress] = field(default=None)
    invalidate_target: Optional[FlashAddress] = field(default=None)
    cache_flush_generated: bool = False
    report_origin_request_ids: list[str] = field(default_factory=list)

    def get_response_from_transaction(self, tr: 'Transaction'):
        if self.type == TransactionType.MAPPING_WRITE and tr.type == TransactionType.MAPPING_READ:
            if tr.response is None:
                raise ValueError("[Transaction] <get_response_from_transaction> mapping read response is empty")
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                self.bitmap[i] = self.bitmap[i] or tr.bitmap[i]
                if self.payload[i] == INVALID_PPA:
                    self.payload[i] = tr.response.data[i]
        elif self.type in [TransactionType.USER_READ, TransactionType.USER_WRITE] and tr.type == TransactionType.MAPPING_READ:
            if tr.failed:
                raise RequestFailure(tr.error_message or "mapping read failed")
            if tr.response is None:
                raise ValueError("[Transaction] <get_response_from_transaction> mapping read response is empty")
            idx = self.lpa % LPA_NO_PER_MAPPING_PAGE
            ppa = tr.response.data[idx]
            if tr.bitmap[idx] == 1 and tr.response.valid_bitmap[idx] == 1 and ppa != INVALID_PPA:
                page_id = ppa % PAGE_PER_BLOCK
                ppa //= PAGE_PER_BLOCK
                sub_plane_id = ppa % BLOCK_PER_PLANE
                ppa //= BLOCK_PER_PLANE
                plane_id = ppa % PLANE_PER_DIE
                ppa //= PLANE_PER_DIE
                die_id = ppa % DIE_PER_CHIP
                ppa //= DIE_PER_CHIP
                chip_id = ppa % CHIP_PER_CHANNEL
                channel_id = ppa // CHIP_PER_CHANNEL
                self.address = FlashAddress(
                    channel=channel_id,
                    chip=chip_id,
                    die=die_id,
                    plane=plane_id,
                    sub_plane=sub_plane_id,
                    page=page_id,
                )
            else:
                raise ValueError(f"[Transaction] <get_response_from_transaction> accessing invalid lpa in mapping page!")
        elif self.type == TransactionType.USER_WRITE and tr.type == TransactionType.USER_READ_FOR_WRITE:
            for i in range(SECTOR_PER_PAGE):
                self.bitmap[i] = self.bitmap[i] or tr.bitmap[i]
                if self.payload[i] == INVALID_DATA:
                    self.payload[i] = tr.payload[i]
        elif self.type == TransactionType.GC_WRITE and tr.type == TransactionType.GC_READ:
            pd = tr.response
            if pd is not None:
                for i in range(SECTOR_PER_PAGE):
                    self.bitmap[i] = self.bitmap[i] or tr.bitmap[i]
                    if hasattr(pd, "data") and pd.data:
                        if self.payload[i] == INVALID_DATA:
                            self.payload[i] = pd.data[i]
        
        return


    def __str__(self) -> str:
        return self.__repr__()
    
    def __repr__(self) -> str:
        req = self.source_req
        source_req_brief = f"Request(type={req.type if req is not None else 'None'}, lha_start={req.lha_start if req is not None else 'None'}, size={req.size if req is not None else 'None'})"
        rely_on_transactions_brief = f"{len(self.rely_on_transactions)} transaction(s)" if self.rely_on_transactions else "[]"
        required_by_transactions_brief = f"{len(self.required_by_transactions)} transaction(s)" if self.required_by_transactions else "[]"
        items = [
            f"type={self.type},",
            f"lpa={self.lpa},",
            f"mvpn={self.mvpn},",
            f"address={repr(self.address)},",
            f"Transaction source_req={source_req_brief},",
            f"bitmap={repr(self.bitmap)},",
            f"payload={repr(self.payload)},",
            f"response={repr(self.response)},",
            f"failed={self.failed},",
            f"error_message={repr(self.error_message)},",
            f"rely_on_transactions={repr(self.rely_on_transactions)},",
            f"required_by_transactions={required_by_transactions_brief},",
            f"completed={self.completed},",
            f"exec_event={repr(self.exec_event)}",
        ]
        return "<" + " ".join(items) + ">"
    
    def __eq__(self, other: 'Transaction') -> bool:
       if self.source_req != other.source_req:
           return False
       if self.type != other.type:
           return False
       if self.lpa != other.lpa:
           return False
       if self.address != other.address:
           return False
       if self.bitmap != other.bitmap:
           return False
       return True

# ----- Request 数据类 -----
@dataclass
class Request:
    """Host 下发的 IO 请求，供 Host、HIL、FTL 使用。"""
    type: RequestType  # READ, WRITE, SEARCH, COMPUTE, MAPPING
    stream_id: int = 0
    sq_id: Optional[int] = None
    transaction_list: List[Transaction] = field(default_factory=list)
    lha_start: int = 0   # start logical sector address
    size: int = 0   # size of request
    issue_time: Optional[int] = None
    finish_time: Optional[int] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    completion_sent: bool = False
    invalidate: Optional[bool] = False
    trace_index: Optional[int] = None
    trace_time: Optional[int] = None
    report_req_id: Optional[str] = None
    report_origin_request_ids: list[str] = field(default_factory=list)

    def is_serviced(self) -> bool:
        """是否所有 transaction 已处理完成。"""
        if not self.transaction_list:
            return True
        for tr in self.transaction_list:
            if not tr.completed:
                return False
        return True

    def __eq__(self, other: object) -> bool:
        """避免 dataclass 自动 __eq__ 遍历 transaction_list 导致循环递归。"""
        if not isinstance(other, Request):
            return NotImplemented
        return self is other or (
            self.type == other.type
            and self.sq_id == other.sq_id
            and self.lha_start == other.lha_start
            and self.size == other.size
            and self.trace_index == other.trace_index
        )

    def __str__(self) -> str:
        return self.__repr__()
    
    def __repr__(self) -> str:
        items = [
            f"Request type={self.type},",
            f"sq_id={self.sq_id},",
            f"transaction_list={self.transaction_list},",
            f"lha_start={self.lha_start},",
            f"size={self.size},",
            f"issue_time={self.issue_time},",
            f"finish_time={self.finish_time},",
            f"status={self.status},",
            f"error_message={repr(self.error_message)},",
            f"completion_sent={self.completion_sent},",
        ]
        return "<" + " ".join(items) + ">"


@dataclass
class FlashAddress:
    channel: int
    chip: int
    die: int
    plane: int
    sub_plane: int
    page: int

    def __str__(self) -> str:
        return self.__repr__()
    def __repr__(self) -> str:
        items = [
            f"FlashAddress channel={self.channel},",
            f"chip={self.chip},",
            f"die={self.die},",
            f"plane={self.plane},",
            f"sub_plane={self.sub_plane},",
            f"page={self.page},",
        ]
        return "<" + " ".join(items) + ">"

# ----- Event 视图（供 execute(event) 使用） -----
@dataclass
class SimEvent:
    """仿真事件：type, target, param。"""
    type: EventType
    target: Any
    time: int
    param: dict[str, Any] = field(default_factory=dict)
    ignored: bool = False

    def __lt__(self, other: 'SimEvent') -> bool:
        return self.time < other.time

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SimEvent):
            return NotImplemented
        return self is other or (
            self.time == other.time
            and self.type == other.type
            and self.target is other.target
        )

    def __str__(self) -> str:
        target_str = f"{type(self.target).__name__}(id={id(self.target)})" if self.target is not None else "None"
        param_str = _format_param(self.param)
        lines = [
            "SimEvent:",
            f"  type:    {self.type}",
            f"  target:  {target_str}",
            f"  time:    {self.time}",
            f"  param:   {param_str}",
            f"  ignored: {self.ignored}",
        ]
        return "\n".join(lines)


def _format_param(param: Any, indent: str = "") -> str:
    """将 param 格式化为可读字符串，若为多行则带缩进。"""
    if param is None:
        return "None"
    if hasattr(param, "__str__") and type(param).__str__ is not object.__str__:
        s = str(param)
        if "\n" in s:
            return "\n" + indent + ("\n" + indent).join(s.split("\n"))
        return s
    return repr(param)


def log_execute_event(module_name: str, event: SimEvent) -> None:
    """在 execute 开始时调用：打印当前模块名和 event 完整信息。"""
    sep = "--------------------------------------------------------"
    debug_info(sep)
    debug_info(f"module <{module_name}> is executing event:")
    debug_info(event)
    debug_info(sep)


def format_event_queue(event_list) -> str:
    """将事件队列内容格式化为多行可读字符串（按 time 排序）。"""
    events = sorted(event_list, key=lambda e: (e.time, id(e)))
    if not events:
        return "Event queue (0 events):\n  (empty)"
    lines = [f"Event queue ({len(events)} events):", ""]
    for i, ev in enumerate(events):
        lines.append(f"========== Event [{i + 1}/{len(events)}] (time={ev.time}) ==========")
        lines.append(str(ev))
        lines.append("")
    return "\n".join(lines)


@dataclass
class cmt_entry:
    address: FlashAddress
    dirty: bool

class GTDEntry:
    def __init__(self, address) -> None:
        self.address = address
    
    def __eq__(self, other: 'GTDEntry') -> bool:
        return self.address == other.address
    
    def __str__(self) -> str:
        lines = [
            "GTDEntry:",
            f"  address:       {self.address}",
        ]
        return "\n".join(lines)
        
# ── Simulation time / event scheduling (set by Engine at startup) ──────────
_time_provider = None       # () -> int   returns current sim time in ns
_event_scheduler = None     # (event_type, target, param, scheduled_time) -> None
_request_latency_recorder = None


def CURRENT_TIME() -> int:
    """Return current simulation time in nanoseconds."""
    if _time_provider is not None:
        return _time_provider()
    raise ValueError("Time provider is not initialized")


def Register_event(event_type: str, target: Any, param: Any, scheduled_time: int) -> None:
    """Register a future simulation event."""
    if _event_scheduler is not None:
        return _event_scheduler(event_type, target, param, scheduled_time)
    else:
        raise ValueError("Event scheduler is not initialized")


# ── Request latency recorder (由 Engine 注入) ─────────────────────────────────

_request_latency_recorder: Any = None


def SET_REQUEST_LATENCY_RECORDER(recorder: Any) -> None:
    global _request_latency_recorder
    _request_latency_recorder = recorder


def REQUEST_LATENCY_RECORDER() -> Any:
    return _request_latency_recorder


if __name__ == "__main__":
    print(STATIC_BASE_LHA)
