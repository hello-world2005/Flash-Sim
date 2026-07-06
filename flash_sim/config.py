"""Configuration classes for flash timing and parallelism parameters."""

import os
from dataclasses import dataclass, field
from typing import Optional, NamedTuple, Dict, Set, List
from enum import Enum


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


DEFAULT_CHANNEL_NO = 8
DEFAULT_CHIP_PER_CHANNEL = 4
DEFAULT_DIES = 4
DEFAULT_PLANES_PER_DIE = 4
DEFAULT_BLOCKS_PER_PLANE = 1024
DEFAULT_LAYERS_PER_BLOCK = 128
DEFAULT_SL_PER_BLOCK = 1
DEFAULT_SSL_PER_SL = 4
DEFAULT_SUB_BLOCKS_PER_BLOCK = DEFAULT_SL_PER_BLOCK * DEFAULT_SSL_PER_SL
DEFAULT_SECTOR_PER_PAGE = 16
DEFAULT_COMPUTE_MAX_PARALLEL_SL = 256
DEFAULT_SEARCH_MAX_PARALLEL_WL = 256
DEFAULT_STATIC_CHIP_PER_CHANNEL = 1
DEFAULT_DATA_CACHE_CAPACITY = 262144

EVENT_RUNTIME_DIES = _env_int("FLASHSIM_EVENT_RUNTIME_DIES", 4)
EVENT_RUNTIME_PLANES_PER_DIE = _env_int("FLASHSIM_EVENT_RUNTIME_PLANES_PER_DIE", 4)
EVENT_RUNTIME_BLOCKS_PER_PLANE = _env_int("FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE", 64)
EVENT_RUNTIME_LAYERS_PER_BLOCK = _env_int("FLASHSIM_EVENT_RUNTIME_LAYERS_PER_BLOCK", 1)
EVENT_RUNTIME_SL_PER_BLOCK = _env_int("FLASHSIM_EVENT_RUNTIME_SL_PER_BLOCK", 2)
EVENT_RUNTIME_SSL_PER_SL = _env_int("FLASHSIM_EVENT_RUNTIME_SSL_PER_SL", 4)
EVENT_RUNTIME_SUB_BLOCKS_PER_BLOCK = EVENT_RUNTIME_SL_PER_BLOCK * EVENT_RUNTIME_SSL_PER_SL


def make_event_runtime_geometry(**overrides) -> "FlashGeometry":
    """Build the compact geometry used by the legacy event-driven runtime."""
    geometry_kwargs = {
        "channel_no": DEFAULT_CHANNEL_NO,
        "chip_per_channel": DEFAULT_CHIP_PER_CHANNEL,
        "dies": EVENT_RUNTIME_DIES,
        "planes_per_die": EVENT_RUNTIME_PLANES_PER_DIE,
        "blocks_per_plane": EVENT_RUNTIME_BLOCKS_PER_PLANE,
        "layers_per_block": EVENT_RUNTIME_LAYERS_PER_BLOCK,
        "sl_per_block": EVENT_RUNTIME_SL_PER_BLOCK,
        "ssl_per_sl": EVENT_RUNTIME_SSL_PER_SL,
        "sub_blocks_per_block": EVENT_RUNTIME_SUB_BLOCKS_PER_BLOCK,
        "sector_per_page": DEFAULT_SECTOR_PER_PAGE,
        "compute_max_parallel_sl": DEFAULT_COMPUTE_MAX_PARALLEL_SL,
        "search_max_parallel_wl": DEFAULT_SEARCH_MAX_PARALLEL_WL,
        "static_chip_per_channel": DEFAULT_STATIC_CHIP_PER_CHANNEL,
    }
    geometry_kwargs.update(overrides)
    return FlashGeometry(**geometry_kwargs)


class FlashTechnology(Enum):
    """NAND Flash memory technology type.

    Different technologies store different numbers of bits per cell:
    - SLC: 1 bit per cell (2 states)
    - MLC: 2 bits per cell (4 states) - LSB and MSB
    - TLC: 3 bits per cell (8 states) - LSB, CSB, and MSB
    """
    SLC = "slc"  # Single-Level Cell: 1 page per block
    MLC = "mlc"  # Multi-Level Cell: 2 pages per block (LSB, MSB)
    TLC = "tlc"  # Triple-Level Cell: 3 pages per block (LSB, CSB, MSB)


class FlashAddress(NamedTuple):
    """Physical flash address components.

    Represents the physical location of a page or block in the flash storage.

    Attributes:
        die: Die index (0-based)
        plane: Plane index within die (0-based)
        block: Block index within plane (0-based)
        layer: Layer index within block for 3D NAND (0-based)
        sub_block: Sub-block index within layer (0-based)
        page: Page index within sub-block (0-based, -1 for block-level ops)
        page_type: Page type for MLC/TLC (0=LSB, 1=CSB, 2=MSB), -1 for block-level ops
    """
    die: int = 0
    plane: int = 0
    block: int = 0
    layer: int = 0  # For 3D NAND, layer index within block
    sub_block: int = 0  # Sub-block index within layer
    page: int = -1  # -1 indicates block-level operation
    page_type: int = 0  # 0=LSB, 1=CSB, 2=MSB (SLC: always 0)

    def __str__(self) -> str:
        if self.page >= 0:
            page_type_str = self.get_page_type_name()
            return (f"Die:{self.die} Plane:{self.plane} Block:{self.block} "
                    f"Layer:{self.layer} SubBlock:{self.sub_block} "
                    f"Page:{self.page} Type:{page_type_str}")
        return f"Die:{self.die} Plane:{self.plane} Block:{self.block} Layer:{self.layer}"

    def get_page_type_name(self) -> str:
        """Get human-readable page type name."""
        if self.page_type == 0:
            return "LSB"
        elif self.page_type == 1:
            return "CSB"
        elif self.page_type == 2:
            return "MSB"
        return "UNKNOWN"


# Page and Block status constants
class PageStatus:
    FREE = "free"
    VALID = "valid"
    INVALID = "invalid"


class BlockStatus:
    FREE = "free"
    USED = "used"
    ERASING = "erasing"


@dataclass
class PageInfo:
    """Information about a single page."""
    status: str = PageStatus.FREE
    lba: Optional[int] = None  # Associated LBA (if mapped)


@dataclass
class BlockInfo:
    """Information about a block."""
    status: str = BlockStatus.FREE
    pe_count: int = 0  # Program-Erase cycle count
    pages: Dict[int, PageInfo] = field(default_factory=dict)  # page_index -> PageInfo

    def __post_init__(self):
        if self.pages is None:
            self.pages = {}


@dataclass
class FlashGeometry:
    """Flash storage geometry configuration for 3D NAND.

    Hierarchical structure:
    - Channel: Independent channel
      - Chip: Flash chip per channel
        - Die: Independent flash die per chip
          - Plane: Share command queue
            - Block: Erase unit
              - Layer: NAND layer within block
                - Sub-Block (SL/SSL): One page per layer (1 page/sub-block/layer)

    Relationship: pages_per_block = layers_per_block * sl_per_block * ssl_per_sl
    (equivalently: sub_blocks_per_block = sl_per_block * ssl_per_sl)
    Each sub-block contains exactly 1 page per layer.

    Default values match the documented public simulator baseline.
    The legacy event-driven runtime uses make_event_runtime_geometry().
    """
    # ----- Channel / chip hierarchy -----
    channel_no: int = DEFAULT_CHANNEL_NO
    chip_per_channel: int = DEFAULT_CHIP_PER_CHANNEL
    dies: int = DEFAULT_DIES
    planes_per_die: int = DEFAULT_PLANES_PER_DIE
    blocks_per_plane: int = DEFAULT_BLOCKS_PER_PLANE
    # ----- Block-local 3D NAND structure -----
    layers_per_block: int = DEFAULT_LAYERS_PER_BLOCK
    sl_per_block: int = DEFAULT_SL_PER_BLOCK
    ssl_per_sl: int = DEFAULT_SSL_PER_SL
    sub_blocks_per_block: int = DEFAULT_SUB_BLOCKS_PER_BLOCK
    sector_per_page: int = DEFAULT_SECTOR_PER_PAGE
    # ----- Search / compute parallelism -----
    compute_max_parallel_sl: int = DEFAULT_COMPUTE_MAX_PARALLEL_SL
    search_max_parallel_wl: int = DEFAULT_SEARCH_MAX_PARALLEL_WL
    static_chip_per_channel: int = DEFAULT_STATIC_CHIP_PER_CHANNEL
    
    # ----- Preconditioning 参数 -----
    valid_invalid_ratio: float = 1.0  # 预条件时每个 block 中有效页占比（1.0=全有效, 0.5=半有效）
    preconditioning_full_block_ratio: float = 0.5  # write_frontier 之外的剩余 block 中，写满 block 的比例（0.0 ~ 1.0）

    def __post_init__(self):
        if self.layers_per_block <= 0:
            raise ValueError("layers_per_block must be positive")
        if self.sub_blocks_per_block <= 0:
            raise ValueError("sub_blocks_per_block must be positive")
        if self.blocks_per_plane <= 0:
            raise ValueError("blocks_per_plane must be positive")
        if self.planes_per_die <= 0:
            raise ValueError("planes_per_die must be positive")
        if self.dies <= 0:
            raise ValueError("dies must be positive")
        if self.channel_no <= 0:
            raise ValueError("channel_no must be positive")
        if self.chip_per_channel <= 0:
            raise ValueError("chip_per_channel must be positive")
        if self.sector_per_page <= 0:
            raise ValueError("sector_per_page must be positive")

    @property
    def pages_per_block(self) -> int:
        """Total pages per block = layers * sub_blocks (1 page per sub-block/layer)."""
        return self.layers_per_block * self.sub_blocks_per_block

    @property
    def pages_per_layer(self) -> int:
        """Pages per layer = number of sub-blocks (1 page per sub-block)."""
        return self.sub_blocks_per_block

    @property
    def layers_per_plane(self) -> int:
        """Total layers per plane."""
        return self.layers_per_block * self.blocks_per_plane

    @property
    def layers_per_die(self) -> int:
        """Total layers per die."""
        return self.layers_per_plane * self.planes_per_die

    @property
    def pages_per_plane(self) -> int:
        """Total pages per plane."""
        return self.pages_per_block * self.blocks_per_plane

    @property
    def pages_per_die(self) -> int:
        """Total pages per die."""
        return self.pages_per_plane * self.planes_per_die

    @property
    def blocks_per_die(self) -> int:
        """Total blocks per die."""
        return self.blocks_per_plane * self.planes_per_die

    @property
    def total_pages(self) -> int:
        """Total pages in the flash storage."""
        return self.pages_per_die * self.dies

    def get_page_type(self, page_in_block: int, technology: FlashTechnology = FlashTechnology.SLC) -> int:
        """Determine page type (LSB/CSB/MSB) based on page index within block.

        For 3D NAND with 1 page per sub-block/layer:
        - SLC: all pages are LSB (page_type=0)
        - MLC: pages within layer alternate LSB/MSB (page 0=LSB, page 1=MSB, page 2=LSB, ...)
        - TLC: pages within layer cycle through LSB/CSB/MSB (page 0=LSB, page 1=CSB, page 2=MSB, page 3=LSB, ...)

        Args:
            page_in_block: Page index within the block (0-based)
            technology: Flash technology type

        Returns:
            Page type: 0=LSB, 1=CSB, 2=MSB
        """
        if technology == FlashTechnology.SLC:
            return 0
        elif technology == FlashTechnology.MLC:
            # MLC: 2 pages per "sub-block layer" group
            # Pages 0,2,4,... = LSB, Pages 1,3,5,... = MSB
            # Even pages (0-indexed) are LSB, odd are MSB
            return page_in_block % 2
        else:  # TLC
            # TLC: 3 pages per "sub-block layer" group
            # Pages 0,3,6,... = LSB, Pages 1,4,7,... = CSB, Pages 2,5,8,... = MSB
            # Cycle through 0, 1, 2, 0, 1, 2, ...
            return page_in_block % 3

    @property
    def total_blocks(self) -> int:
        """Total blocks in the flash storage."""
        return self.blocks_per_die * self.dies

    # ----- 与 common.py 对应的派生量（计算/搜索 Bank、随机扇区总数） -----
    @property
    def page_no_per_search_bank(self) -> int:
        """Search bank 内并行页数（= search_max_parallel_wl）。"""
        return self.search_max_parallel_wl

    @property
    def page_no_per_compute_bank(self) -> int:
        """Compute bank 内并行页数（= compute_max_parallel_sl * ssl_per_sl）。"""
        return self.compute_max_parallel_sl * self.ssl_per_sl

    @property
    def compute_bank_per_plane(self) -> int:
        """每 Plane 的 Compute Bank 数量。"""
        return self.blocks_per_plane * self.sl_per_block // self.compute_max_parallel_sl

    @property
    def search_bank_per_plane(self) -> int:
        """每 Plane 的 Search Bank 数量。"""
        return self.ssl_per_sl * self.sl_per_block * self.blocks_per_plane

    @property
    def tot_random_sector_no(self) -> int:
        """可用于随机访问的扇区总数（不含 static chip 部分）。"""
        return (
            self.sector_per_page * self.pages_per_block * self.blocks_per_plane
            * self.planes_per_die * self.dies * self.channel_no
            * (self.chip_per_channel - self.static_chip_per_channel)
        )
    
    @property
    def static_area_base_address(self) -> int:
        """static area 的起始地址。"""
        return self.tot_random_sector_no//self.sector_per_page

    def page_to_address(self, page: int, technology: FlashTechnology = FlashTechnology.SLC) -> FlashAddress:
        """Convert a linear page number to physical flash address.

        Each sub-block contains exactly 1 page per layer.
        pages_per_block = layers * sub_blocks.

        Args:
            page: Linear page number (0-based).
            technology: Flash technology type (affects page_type).

        Returns:
            FlashAddress representing the physical location.

        Raises:
            ValueError: If page number exceeds total pages.
        """
        if page < 0 or page >= self.total_pages:
            raise ValueError(f"Page {page} out of range [0, {self.total_pages})")

        remaining = page
        die = remaining // self.pages_per_die
        remaining %= self.pages_per_die
        plane = remaining // self.pages_per_plane
        remaining %= self.pages_per_plane
        block = remaining // self.pages_per_block
        remaining %= self.pages_per_block
        layer = remaining // self.sub_blocks_per_block
        remaining %= self.sub_blocks_per_block
        sub_block = remaining  # Each sub-block has 1 page

        # Calculate page_type based on technology
        page_type = self.get_page_type(layer * self.sub_blocks_per_block + sub_block, technology)

        return FlashAddress(die=die, plane=plane, block=block,
                           layer=layer, sub_block=sub_block, page=0, page_type=page_type)

    def block_to_address(self, block: int) -> FlashAddress:
        """Convert a linear block number to physical flash address.

        Args:
            block: Linear block number (0-based).

        Returns:
            FlashAddress representing the physical location.

        Raises:
            ValueError: If block number exceeds total blocks.
        """
        if block < 0 or block >= self.total_blocks:
            raise ValueError(f"Block {block} out of range [0, {self.total_blocks})")

        remaining = block
        die = remaining // self.blocks_per_die
        remaining %= self.blocks_per_die
        plane = remaining // self.blocks_per_plane
        block_in_plane = remaining % self.blocks_per_plane

        return FlashAddress(die=die, plane=plane, block=block_in_plane, page=-1)

    def address_to_page(self, addr: FlashAddress) -> int:
        """Convert physical flash address to linear page number.

        Args:
            addr: FlashAddress representing the physical location.

        Returns:
            Linear page number.

        Raises:
            ValueError: If address components are out of bounds.
        """
        if addr.page < 0:
            raise ValueError("Cannot convert block address to page number")

        if addr.die >= self.dies:
            raise ValueError(f"Die {addr.die} out of range [0, {self.dies})")
        if addr.plane >= self.planes_per_die:
            raise ValueError(f"Plane {addr.plane} out of range [0, {self.planes_per_die})")
        if addr.block >= self.blocks_per_plane:
            raise ValueError(f"Block {addr.block} out of range [0, {self.blocks_per_plane})")
        if addr.layer >= self.layers_per_block:
            raise ValueError(f"Layer {addr.layer} out of range [0, {self.layers_per_block})")
        if addr.sub_block >= self.sub_blocks_per_block:
            raise ValueError(f"Sub-block {addr.sub_block} out of range [0, {self.sub_blocks_per_block})")

        return ((addr.die * self.pages_per_die) +
                (addr.plane * self.pages_per_plane) +
                (addr.block * self.pages_per_block) +
                (addr.layer * self.sub_blocks_per_block) +
                addr.sub_block)

    def address_to_block(self, addr: FlashAddress) -> int:
        """Convert physical flash address to linear block number.

        Args:
            addr: FlashAddress representing the physical location.

        Returns:
            Linear block number.
        """
        if addr.die >= self.dies:
            raise ValueError(f"Die {addr.die} out of range [0, {self.dies})")
        if addr.plane >= self.planes_per_die:
            raise ValueError(f"Plane {addr.plane} out of range [0, {self.planes_per_die})")
        if addr.block >= self.blocks_per_plane:
            raise ValueError(f"Block {addr.block} out of range [0, {self.blocks_per_plane})")

        return ((addr.die * self.blocks_per_die) +
                (addr.plane * self.blocks_per_plane) +
                addr.block)


@dataclass
class TimingConfig:
    """NAND Flash timing parameters in nanoseconds.

    For MLC and TLC flash, different page types (LSB/CSB/MSB) have
    different read and program latencies.

    Default values based on standard NAND Flash specifications.
    """
    # Technology type (SLC/MLC/TLC)
    technology: FlashTechnology = FlashTechnology.SLC

    # Read latencies (tR) in nanoseconds
    t_r_lsb: int = 5_000    # LSB page read latency
    t_r_csb: int = 100_000   # CSB page read latency (MLC/TLC only)
    t_r_msb: int = 150_000   # MSB page read latency (MLC/TLC only)

    # Program latencies (tPROG) in nanoseconds
    t_prog_lsb: int = 250_000   # LSB page program latency
    t_prog_csb: int = 1_000_000 # CSB page program latency (MLC/TLC only)
    t_prog_msb: int = 1_500_000 # MSB page program latency (MLC/TLC only)

    # Erase latency (tBERS) - same for all page types
    t_bers: int = 10_000_000  # Block erase latency: 10 milliseconds

    # Search latency (tSEARCH) in nanoseconds
    t_search_lsb: int = 200_000   # LSB search latency

    # Compute latency (tCOMPUTE) in nanoseconds
    t_compute_lsb: int = 500_000   # LSB compute latency

    def __post_init__(self):
        if not isinstance(self.technology, FlashTechnology):
            raise ValueError("technology must be a FlashTechnology enum value")

        # Validate read latencies
        if self.t_r_lsb <= 0:
            raise ValueError("t_r_lsb must be positive")
        if self.technology in (FlashTechnology.MLC, FlashTechnology.TLC):
            if self.t_r_csb <= 0:
                raise ValueError("t_r_csb must be positive for MLC/TLC")
            if self.t_r_msb <= 0:
                raise ValueError("t_r_msb must be positive for MLC/TLC")

        # Validate program latencies
        if self.t_prog_lsb <= 0:
            raise ValueError("t_prog_lsb must be positive")
        if self.technology in (FlashTechnology.MLC, FlashTechnology.TLC):
            if self.t_prog_csb <= 0:
                raise ValueError("t_prog_csb must be positive for MLC/TLC")
            if self.t_prog_msb <= 0:
                raise ValueError("t_prog_msb must be positive for MLC/TLC")

        # Validate erase latency
        if self.t_bers <= 0:
            raise ValueError("t_bers must be positive")

    def get_read_latency(self, page_type: int) -> int:
        """Get read latency based on page type.

        Args:
            page_type: Page type index (0=LSB, 1=CSB, 2=MSB)

        Returns:
            Read latency in nanoseconds
        """
        # SLC always uses LSB latency regardless of page_type
        if self.technology == FlashTechnology.SLC or page_type == 0:
            return self.t_r_lsb
        elif page_type == 1:
            # CSB for TLC, MSB for MLC
            if self.technology == FlashTechnology.TLC:
                return self.t_r_csb
            else:  # MLC: page_type=1 is MSB
                return self.t_r_msb
        else:  # page_type >= 2 (only possible for TLC)
            return self.t_r_msb

    def get_program_latency(self, page_type: int) -> int:
        """Get program latency based on page type.

        Args:
            page_type: Page type index (0=LSB, 1=CSB, 2=MSB)

        Returns:
            Program latency in nanoseconds
        """
        # SLC always uses LSB latency regardless of page_type
        if self.technology == FlashTechnology.SLC or page_type == 0:
            return self.t_prog_lsb
        elif page_type == 1:
            # CSB for TLC, MSB for MLC
            if self.technology == FlashTechnology.TLC:
                return self.t_prog_csb
            else:  # MLC: page_type=1 is MSB
                return self.t_prog_msb
        else:  # page_type >= 2 (only possible for TLC)
            return self.t_prog_msb


@dataclass
class OnfiTimingConfig:
    """ONFI NVDDR2 channel timing parameters in nanoseconds."""

    channel_width_bytes: int = 8
    t_rc: int = 6
    t_dsc: int = 6
    t_dbsy: int = 500
    t_cs: int = 20
    t_rr: int = 20
    t_wb: int = 100
    t_wc: int = 25
    t_adl: int = 70
    t_cals: int = 15
    t_dqsre: int = 15
    t_rpre: int = 15
    t_rhw: int = 100
    t_ccs: int = 300
    t_wpst: int = 6
    t_wpsth: int = 15

    def __post_init__(self):
        for field_name in (
            "channel_width_bytes",
            "t_rc",
            "t_dsc",
            "t_dbsy",
            "t_cs",
            "t_rr",
            "t_wb",
            "t_wc",
            "t_adl",
            "t_cals",
            "t_dqsre",
            "t_rpre",
            "t_rhw",
            "t_ccs",
            "t_wpst",
            "t_wpsth",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")

    @property
    def two_unit_data_in_time(self) -> int:
        return self.t_rc

    @property
    def two_unit_data_out_time(self) -> int:
        return self.t_dsc


@dataclass
class ParallelConfig:
    """Parallelism configuration for search and compute operations."""
    max_parallel_wl: int = 64    # Maximum parallel Word Lines for search
    max_parallel_blocks: int = 8  # Maximum parallel Blocks for compute

    def __post_init__(self):
        if self.max_parallel_wl <= 0:
            raise ValueError("max_parallel_wl must be positive")
        if self.max_parallel_blocks <= 0:
            raise ValueError("max_parallel_blocks must be positive")


@dataclass
class RuntimeConfig:
    """Event-driven runtime policy knobs."""

    gc_low_watermark: int = 3
    gc_exec_threshold: float | None = None
    stop_servicing_writes_threshold: int = 1
    gc_reserve_blocks: int = 1
    gc_min_invalid_pages: int = 1
    gc_min_invalid_ratio: float = 0.0
    gc_emergency_watermark: int = 1
    gc_victim_policy: str = "greedy"
    gc_d_choices: int = 10
    gc_random_seed: int = 42
    static_wl_enabled: bool = True
    static_wl_wear_gap_threshold: int = 2
    cache_bypass: bool = False
    data_cache_capacity: int = DEFAULT_DATA_CACHE_CAPACITY
    precondition_fill_ratio: float | None = None
    precondition_mode: str = "capacity-fill"
    precondition_seed: int = 42
    plane_allocation: str = "PAGE_LEVEL"  # "PAGE_LEVEL" or "CWDP"

    @property
    def plane_allocation_scheme(self) -> str:
        return self.plane_allocation

    @plane_allocation_scheme.setter
    def plane_allocation_scheme(self, value: str) -> None:
        self.plane_allocation = value

    def __post_init__(self):
        if self.gc_low_watermark < 0:
            raise ValueError("gc_low_watermark must be non-negative")
        if self.gc_exec_threshold is not None and not (0.0 <= self.gc_exec_threshold <= 1.0):
            raise ValueError("gc_exec_threshold must be between 0.0 and 1.0")
        if self.stop_servicing_writes_threshold < 0:
            raise ValueError("stop_servicing_writes_threshold must be non-negative")
        if self.gc_reserve_blocks < 0:
            raise ValueError("gc_reserve_blocks must be non-negative")
        if self.gc_min_invalid_pages < 0:
            raise ValueError("gc_min_invalid_pages must be non-negative")
        if not (0.0 <= self.gc_min_invalid_ratio <= 1.0):
            raise ValueError("gc_min_invalid_ratio must be between 0.0 and 1.0")
        if self.gc_emergency_watermark < 0:
            raise ValueError("gc_emergency_watermark must be non-negative")
        policy_aliases = {
            "greedy": "greedy",
            "d-choices": "d-choices",
            "d_choices": "d-choices",
            "dchoices": "d-choices",
            "rga": "d-choices",
        }
        policy = policy_aliases.get(str(self.gc_victim_policy).lower())
        if policy is None:
            raise ValueError("gc_victim_policy must be greedy, d-choices, or rga")
        self.gc_victim_policy = policy
        if self.gc_d_choices <= 0:
            raise ValueError("gc_d_choices must be positive")
        if self.static_wl_wear_gap_threshold < 0:
            raise ValueError("static_wl_wear_gap_threshold must be non-negative")
        if self.data_cache_capacity <= 0:
            raise ValueError("data_cache_capacity must be positive")
        if self.precondition_fill_ratio is not None and not (0.0 <= self.precondition_fill_ratio <= 1.0):
            raise ValueError("precondition_fill_ratio must be between 0.0 and 1.0")
        if self.precondition_mode not in ("capacity-fill", "trace-cover"):
            raise ValueError("precondition_mode must be capacity-fill or trace-cover")
        if self.plane_allocation not in ("PAGE_LEVEL", "CWDP"):
            raise ValueError("plane_allocation must be PAGE_LEVEL or CWDP")


@dataclass
class FlashConfig:
    """Complete flash simulator configuration."""
    timing: TimingConfig = field(default_factory=TimingConfig)
    onfi: OnfiTimingConfig = field(default_factory=OnfiTimingConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    geometry: FlashGeometry = field(default_factory=FlashGeometry)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @classmethod
    def from_dict(cls, config_dict: dict) -> "FlashConfig":
        """Create configuration from a dictionary."""
        timing_dict = config_dict.get("timing", {})
        onfi_dict = config_dict.get("onfi", {})
        parallel_dict = config_dict.get("parallel", {})
        geometry_dict = config_dict.get("geometry", {})
        runtime_dict = dict(config_dict.get("runtime", config_dict.get("gc", {})))
        for key in (
            "gc_low_watermark",
            "gc_exec_threshold",
            "stop_servicing_writes_threshold",
            "gc_reserve_blocks",
            "gc_min_invalid_pages",
            "gc_min_invalid_ratio",
            "gc_emergency_watermark",
            "gc_victim_policy",
            "gc_d_choices",
            "gc_random_seed",
            "static_wl_enabled",
            "static_wl_wear_gap_threshold",
            "cache_bypass",
            "cache_cap",
            "cache_capacity",
            "data_cache_capacity",
            "precondition_fill_ratio",
            "precondition_mode",
            "precondition_seed",
            "plane_allocation",
            "plane_allocation_scheme",
        ):
            if key in config_dict and key not in runtime_dict:
                runtime_dict[key] = config_dict[key]

        # Parse technology
        tech_str = timing_dict.get("technology", "slc").lower()
        if tech_str == "mlc":
            technology = FlashTechnology.MLC
        elif tech_str == "tlc":
            technology = FlashTechnology.TLC
        else:
            technology = FlashTechnology.SLC

        timing_defaults = TimingConfig()
        timing = TimingConfig(
            technology=technology,
            t_r_lsb=timing_dict.get("t_r_lsb", timing_defaults.t_r_lsb),
            t_r_csb=timing_dict.get("t_r_csb", timing_defaults.t_r_csb),
            t_r_msb=timing_dict.get("t_r_msb", timing_defaults.t_r_msb),
            t_prog_lsb=timing_dict.get("t_prog_lsb", timing_defaults.t_prog_lsb),
            t_prog_csb=timing_dict.get("t_prog_csb", timing_defaults.t_prog_csb),
            t_prog_msb=timing_dict.get("t_prog_msb", timing_defaults.t_prog_msb),
            t_bers=timing_dict.get("t_bers", timing_defaults.t_bers),
        )

        onfi = OnfiTimingConfig(
            channel_width_bytes=onfi_dict.get("channel_width_bytes", 8),
            t_rc=onfi_dict.get("t_rc", 6),
            t_dsc=onfi_dict.get("t_dsc", 6),
            t_dbsy=onfi_dict.get("t_dbsy", 500),
            t_cs=onfi_dict.get("t_cs", 20),
            t_rr=onfi_dict.get("t_rr", 20),
            t_wb=onfi_dict.get("t_wb", 100),
            t_wc=onfi_dict.get("t_wc", 25),
            t_adl=onfi_dict.get("t_adl", 70),
            t_cals=onfi_dict.get("t_cals", 15),
            t_dqsre=onfi_dict.get("t_dqsre", 15),
            t_rpre=onfi_dict.get("t_rpre", 15),
            t_rhw=onfi_dict.get("t_rhw", 100),
            t_ccs=onfi_dict.get("t_ccs", 300),
            t_wpst=onfi_dict.get("t_wpst", 6),
            t_wpsth=onfi_dict.get("t_wpsth", 15),
        )

        parallel = ParallelConfig(
            max_parallel_wl=parallel_dict.get("max_parallel_wl", 64),
            max_parallel_blocks=parallel_dict.get("max_parallel_blocks", 8),
        )

        geometry = FlashGeometry(
            channel_no=geometry_dict.get("channel_no", DEFAULT_CHANNEL_NO),
            chip_per_channel=geometry_dict.get("chip_per_channel", DEFAULT_CHIP_PER_CHANNEL),
            layers_per_block=geometry_dict.get("layers_per_block", DEFAULT_LAYERS_PER_BLOCK),
            sl_per_block=geometry_dict.get("sl_per_block", DEFAULT_SL_PER_BLOCK),
            ssl_per_sl=geometry_dict.get("ssl_per_sl", DEFAULT_SSL_PER_SL),
            sub_blocks_per_block=geometry_dict.get(
                "sub_blocks_per_block", DEFAULT_SUB_BLOCKS_PER_BLOCK
            ),
            blocks_per_plane=geometry_dict.get("blocks_per_plane", DEFAULT_BLOCKS_PER_PLANE),
            planes_per_die=geometry_dict.get("planes_per_die", DEFAULT_PLANES_PER_DIE),
            dies=geometry_dict.get("dies", DEFAULT_DIES),
            sector_per_page=geometry_dict.get("sector_per_page", DEFAULT_SECTOR_PER_PAGE),
            compute_max_parallel_sl=geometry_dict.get(
                "compute_max_parallel_sl", DEFAULT_COMPUTE_MAX_PARALLEL_SL
            ),
            search_max_parallel_wl=geometry_dict.get(
                "search_max_parallel_wl", DEFAULT_SEARCH_MAX_PARALLEL_WL
            ),
            static_chip_per_channel=geometry_dict.get(
                "static_chip_per_channel", DEFAULT_STATIC_CHIP_PER_CHANNEL
            ),
        )

        runtime = RuntimeConfig(
            gc_low_watermark=runtime_dict.get("gc_low_watermark", 3),
            gc_exec_threshold=runtime_dict.get("gc_exec_threshold"),
            stop_servicing_writes_threshold=runtime_dict.get(
                "stop_servicing_writes_threshold", 1
            ),
            gc_reserve_blocks=runtime_dict.get("gc_reserve_blocks", 1),
            gc_min_invalid_pages=runtime_dict.get("gc_min_invalid_pages", 1),
            gc_min_invalid_ratio=runtime_dict.get("gc_min_invalid_ratio", 0.0),
            gc_emergency_watermark=runtime_dict.get(
                "gc_emergency_watermark",
                runtime_dict.get("stop_servicing_writes_threshold", 1),
            ),
            gc_victim_policy=runtime_dict.get("gc_victim_policy", "greedy"),
            gc_d_choices=runtime_dict.get("gc_d_choices", 10),
            gc_random_seed=runtime_dict.get("gc_random_seed", 42),
            static_wl_enabled=runtime_dict.get("static_wl_enabled", True),
            static_wl_wear_gap_threshold=runtime_dict.get(
                "static_wl_wear_gap_threshold", 2
            ),
            cache_bypass=runtime_dict.get("cache_bypass", False),
            data_cache_capacity=runtime_dict.get(
                "data_cache_capacity",
                runtime_dict.get(
                    "cache_capacity",
                    runtime_dict.get("cache_cap", DEFAULT_DATA_CACHE_CAPACITY),
                ),
            ),
            precondition_fill_ratio=runtime_dict.get("precondition_fill_ratio"),
            precondition_mode=runtime_dict.get("precondition_mode", "capacity-fill"),
            precondition_seed=runtime_dict.get("precondition_seed", 42),
            plane_allocation=runtime_dict.get(
                "plane_allocation",
                runtime_dict.get("plane_allocation_scheme", "PAGE_LEVEL"),
            ),
        )

        return cls(
            timing=timing,
            onfi=onfi,
            parallel=parallel,
            geometry=geometry,
            runtime=runtime,
        )

    def to_dict(self) -> dict:
        """Convert configuration to a dictionary."""
        return {
            "timing": {
                "technology": self.timing.technology.value,
                "t_r_lsb": self.timing.t_r_lsb,
                "t_r_csb": self.timing.t_r_csb,
                "t_r_msb": self.timing.t_r_msb,
                "t_prog_lsb": self.timing.t_prog_lsb,
                "t_prog_csb": self.timing.t_prog_csb,
                "t_prog_msb": self.timing.t_prog_msb,
                "t_bers": self.timing.t_bers,
            },
            "onfi": {
                "channel_width_bytes": self.onfi.channel_width_bytes,
                "t_rc": self.onfi.t_rc,
                "t_dsc": self.onfi.t_dsc,
                "t_dbsy": self.onfi.t_dbsy,
                "t_cs": self.onfi.t_cs,
                "t_rr": self.onfi.t_rr,
                "t_wb": self.onfi.t_wb,
                "t_wc": self.onfi.t_wc,
                "t_adl": self.onfi.t_adl,
                "t_cals": self.onfi.t_cals,
                "t_dqsre": self.onfi.t_dqsre,
                "t_rpre": self.onfi.t_rpre,
                "t_rhw": self.onfi.t_rhw,
                "t_ccs": self.onfi.t_ccs,
                "t_wpst": self.onfi.t_wpst,
                "t_wpsth": self.onfi.t_wpsth,
            },
            "parallel": {
                "max_parallel_wl": self.parallel.max_parallel_wl,
                "max_parallel_blocks": self.parallel.max_parallel_blocks,
            },
            "geometry": {
                "channel_no": self.geometry.channel_no,
                "chip_per_channel": self.geometry.chip_per_channel,
                "layers_per_block": self.geometry.layers_per_block,
                "sl_per_block": self.geometry.sl_per_block,
                "ssl_per_sl": self.geometry.ssl_per_sl,
                "sub_blocks_per_block": self.geometry.sub_blocks_per_block,
                "blocks_per_plane": self.geometry.blocks_per_plane,
                "planes_per_die": self.geometry.planes_per_die,
                "dies": self.geometry.dies,
                "sector_per_page": self.geometry.sector_per_page,
                "compute_max_parallel_sl": self.geometry.compute_max_parallel_sl,
                "search_max_parallel_wl": self.geometry.search_max_parallel_wl,
                "static_chip_per_channel": self.geometry.static_chip_per_channel,
            },
            "runtime": {
                "gc_low_watermark": self.runtime.gc_low_watermark,
                "gc_exec_threshold": self.runtime.gc_exec_threshold,
                "stop_servicing_writes_threshold": self.runtime.stop_servicing_writes_threshold,
                "gc_reserve_blocks": self.runtime.gc_reserve_blocks,
                "gc_min_invalid_pages": self.runtime.gc_min_invalid_pages,
                "gc_min_invalid_ratio": self.runtime.gc_min_invalid_ratio,
                "gc_emergency_watermark": self.runtime.gc_emergency_watermark,
                "gc_victim_policy": self.runtime.gc_victim_policy,
                "gc_d_choices": self.runtime.gc_d_choices,
                "gc_random_seed": self.runtime.gc_random_seed,
                "static_wl_enabled": self.runtime.static_wl_enabled,
                "static_wl_wear_gap_threshold": self.runtime.static_wl_wear_gap_threshold,
                "cache_bypass": self.runtime.cache_bypass,
                "data_cache_capacity": self.runtime.data_cache_capacity,
                "precondition_fill_ratio": self.runtime.precondition_fill_ratio,
                "precondition_mode": self.runtime.precondition_mode,
                "precondition_seed": self.runtime.precondition_seed,
                "plane_allocation": self.runtime.plane_allocation,
                "plane_allocation_scheme": self.runtime.plane_allocation_scheme,
            },
        }

    # Convenience properties for backward compatibility
    @property
    def pages_per_block(self) -> int:
        return self.geometry.pages_per_block

    @property
    def blocks_per_plane(self) -> int:
        return self.geometry.blocks_per_plane

    @property
    def planes_per_die(self) -> int:
        return self.geometry.planes_per_die


class FTL:
    """Flash Translation Layer - LBA to Physical Address Mapping.

    Implements logical to physical address translation for flash storage.
    Each LBA maps to one Page (1 LBA = 1 Page).

    Architecture:
        Host LBA (0, 1, 2, ..., N-1)
            │
            └── FTL (Translation Layer)
                │
                └── Physical Flash Address
                    ├── Die (independent chip)
                    ├── Plane (shared command queue)
                    ├── Block (erase unit)
                    └── Page (read/write unit)
    """

    def __init__(self, geometry: FlashGeometry):
        """Initialize FTL with flash geometry.

        Args:
            geometry: Flash storage geometry configuration.
        """
        self.geometry = geometry
        self._l2p_table: Dict[int, FlashAddress] = {}  # LBA → Physical Address
        self._blocks: Dict[FlashAddress, BlockInfo] = {}  # Physical → Block Info
        self._total_lbas = geometry.total_pages
        self._init_blocks()

    def _init_blocks(self):
        """Initialize block information for all blocks."""
        for block_idx in range(self.geometry.total_blocks):
            addr = self.geometry.block_to_address(block_idx)
            self._blocks[addr] = BlockInfo()

    @property
    def total_lbas(self) -> int:
        """Total number of supported LBAs."""
        return self._total_lbas

    @property
    def valid_lbas(self) -> Set[int]:
        """Set of LBAs with valid mappings."""
        return set(self._l2p_table.keys())

    @property
    def block_count(self) -> int:
        """Total number of blocks."""
        return self.geometry.total_blocks

    def lba_to_block(self, lba: int) -> FlashAddress:
        """Convert LBA to block address.

        Args:
            lba: Logical Block Address (maps to a page).

        Returns:
            Physical block address containing this LBA.
        """
        if lba < 0 or lba >= self._total_lbas:
            raise ValueError(f"LBA {lba} out of range [0, {self._total_lbas})")

        page_addr = self.geometry.page_to_address(lba)
        return self.geometry.block_to_address(
            self.geometry.address_to_block(page_addr)
        )

    def lba_to_address(self, lba: int) -> FlashAddress:
        """Convert LBA to full physical page address.

        Args:
            lba: Logical Block Address.

        Returns:
            Physical page address.
        """
        if lba < 0 or lba >= self._total_lbas:
            raise ValueError(f"LBA {lba} out of range [0, {self._total_lbas})")

        return self.geometry.page_to_address(lba)

    def block_range(self, start_lba: int, block_count: int) -> List[FlashAddress]:
        """Get a range of block addresses starting from LBA.

        Args:
            start_lba: Starting LBA (converted to block).
            block_count: Number of blocks to include.

        Returns:
            List of physical block addresses.
        """
        start_block = self.lba_to_block(start_lba)
        blocks = []
        start_linear = self.geometry.address_to_block(start_block)

        for i in range(block_count):
            block_linear = start_linear + i
            if block_linear >= self.geometry.total_blocks:
                raise ValueError(
                    f"Block range exceeds total blocks ({self.geometry.total_blocks})"
                )
            addr = self.geometry.block_to_address(block_linear)
            blocks.append(addr)

        return blocks

    def get_physical_address(self, lba: int) -> Optional[FlashAddress]:
        """Get physical address for a logical block address.

        Args:
            lba: Logical Block Address.

        Returns:
            Physical flash address, or None if LBA not mapped.
        """
        if lba < 0 or lba >= self._total_lbas:
            raise ValueError(f"LBA {lba} out of range [0, {self._total_lbas})")

        return self._l2p_table.get(lba)

    def get_block_info(self, addr: FlashAddress) -> BlockInfo:
        """Get information about a block.

        Args:
            addr: Physical block address.

        Returns:
            BlockInfo for the block.
        """
        if addr.page != -1:
            block_linear = self.geometry.address_to_block(addr)
            addr = self.geometry.block_to_address(block_linear)
        return self._blocks.get(addr, BlockInfo())

    def read(self, lba: int) -> Optional[FlashAddress]:
        """Read mapping - get physical address for LBA.

        Args:
            lba: Logical Block Address.

        Returns:
            Physical address if LBA is mapped and valid, None otherwise.
        """
        pba = self._l2p_table.get(lba)
        if pba is None:
            return None

        # Check if page is still valid
        block_addr = self.geometry.block_to_address(
            self.geometry.address_to_block(pba)
        )
        block_info = self._blocks.get(block_addr)
        if block_info:
            page_info = block_info.pages.get(pba.page, None)
            if page_info and page_info.status == PageStatus.INVALID:
                return None

        return pba

    def write(self, lba: int, pba: FlashAddress) -> None:
        """Write mapping - associate LBA with physical address.

        Args:
            lba: Logical Block Address.
            pba: Physical page address (must have valid page index).
        """
        if lba in self._l2p_table:
            # Invalidate old mapping
            old_pba = self._l2p_table[lba]
            self._invalidate_page(old_pba)

        self._l2p_table[lba] = pba

        # Update block page info
        block_addr = self.geometry.block_to_address(
            self.geometry.address_to_block(pba)
        )
        block_info = self._blocks.get(block_addr)
        if block_info:
            if pba.page not in block_info.pages:
                block_info.pages[pba.page] = PageInfo()
            block_info.pages[pba.page].status = PageStatus.VALID
            block_info.pages[pba.page].lba = lba
            block_info.status = BlockStatus.USED

    def erase(self, block_addr: FlashAddress) -> None:
        """Erase a block - clear all mappings in the block.

        Args:
            block_addr: Physical block address to erase.
        """
        block_info = self.get_block_info(block_addr)
        block_info.status = BlockStatus.FREE
        block_info.pe_count += 1

        # Clear all pages in this block
        block_info.pages = {}

        # Remove mappings for LBAs that were in this block
        lbas_to_remove = []
        for lba, pba in self._l2p_table.items():
            if self.geometry.address_to_block(pba) == self.geometry.address_to_block(block_addr):
                lbas_to_remove.append(lba)

        for lba in lbas_to_remove:
            del self._l2p_table[lba]

    def _invalidate_page(self, pba: FlashAddress) -> None:
        """Mark a page as invalid.

        Args:
            pba: Physical page address to invalidate.
        """
        block_addr = self.geometry.block_to_address(
            self.geometry.address_to_block(pba)
        )
        block_info = self._blocks.get(block_addr)
        if block_info and pba.page in block_info.pages:
            block_info.pages[pba.page].status = PageStatus.INVALID

    def get_pe_count(self, block_addr: FlashAddress) -> int:
        """Get program/erase count for a block.

        Args:
            block_addr: Physical block address.

        Returns:
            Number of erase cycles for the block.
        """
        block_info = self.get_block_info(block_addr)
        return block_info.pe_count

    def get_wear_info(self) -> Dict[str, int]:
        """Get wear information for all blocks.

        Returns:
            Dictionary with min, max, and average PE counts.
        """
        if not self._blocks:
            return {"min": 0, "max": 0, "avg": 0}

        pe_counts = [info.pe_count for info in self._blocks.values()]
        return {
            "min": min(pe_counts),
            "max": max(pe_counts),
            "avg": sum(pe_counts) // len(pe_counts)
        }
