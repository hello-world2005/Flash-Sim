"""Flash chip model with timing calculations for all operations."""

from dataclasses import dataclass
from typing import Optional
from .config import FlashConfig, TimingConfig, ParallelConfig, FlashGeometry, FlashAddress, FlashTechnology


class FlashChip:
    """Simulates a NAND Flash chip with cycle-accurate timing.

    Supports basic storage operations (read, write, erase) and
    advanced operations (search with parallel WL, compute with parallel blocks).

    For MLC and TLC flash, different page types (LSB/CSB/MSB) have
    different read and program latencies.
    """

    def __init__(self, config: Optional[FlashConfig] = None):
        """Initialize flash chip with configuration.

        Args:
            config: Flash configuration. Uses the documented public defaults
                if not provided.
        """
        self._construction_valid: bool = False
        self.config = config or FlashConfig()

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.config is not None, "FlashChip config is not set"
        self._construction_valid = True

    @property
    def timing(self) -> TimingConfig:
        """Get timing configuration."""
        return self.config.timing

    @property
    def parallel(self) -> ParallelConfig:
        """Get parallel configuration."""
        return self.config.parallel

    @property
    def geometry(self) -> FlashGeometry:
        """Get geometry configuration."""
        return self.config.geometry

    @property
    def technology(self) -> FlashTechnology:
        """Get flash technology type."""
        return self.timing.technology

    def page_to_address(self, page: int) -> FlashAddress:
        """Convert a linear page number to physical flash address.

        Args:
            page: Linear page number (0-based).

        Returns:
            FlashAddress representing the physical location.

        Raises:
            ValueError: If page number is out of range.
        """
        return self.geometry.page_to_address(page, self.technology)

    def block_to_address(self, block: int) -> FlashAddress:
        """Convert a linear block number to physical flash address.

        Args:
            block: Linear block number (0-based).

        Returns:
            FlashAddress representing the physical location.

        Raises:
            ValueError: If block number is out of range.
        """
        return self.geometry.block_to_address(block)

    def get_read_latency(self, address: int) -> int:
        """Calculate read operation latency based on page type.

        For MLC/TLC flash, LSB pages have different latency than CSB/MSB pages.

        Args:
            address: Page address to read from (used to determine page type).

        Returns:
            Latency in nanoseconds (tR).
        """
        # Get physical address to determine page type
        addr = self.page_to_address(address)
        return self.timing.get_read_latency(addr.page_type)

    def get_write_latency(self, address: int) -> int:
        """Calculate write (program) operation latency based on page type.

        For MLC/TLC flash, LSB pages have different latency than CSB/MSB pages.

        Args:
            address: Page address to write to (used to determine page type).

        Returns:
            Latency in nanoseconds (tPROG).
        """
        # Get physical address to determine page type
        addr = self.page_to_address(address)
        return self.timing.get_program_latency(addr.page_type)

    def get_erase_latency(self, block_address: int) -> int:
        """Calculate erase operation latency.

        Erase latency is the same regardless of page type.

        Args:
            block_address: Block address to erase.

        Returns:
            Latency in nanoseconds (tBERS).
        """
        return self.timing.t_bers

    def get_read_latency_by_page_type(self, page_type: int) -> int:
        """Get read latency based on page type.

        Args:
            page_type: Page type (0=LSB, 1=CSB, 2=MSB)

        Returns:
            Read latency in nanoseconds
        """
        return self.timing.get_read_latency(page_type)

    def get_program_latency_by_page_type(self, page_type: int) -> int:
        """Get program latency based on page type.

        Args:
            page_type: Page type (0=LSB, 1=CSB, 2=MSB)

        Returns:
            Program latency in nanoseconds
        """
        return self.timing.get_program_latency(page_type)

    def get_search_latency(self, wl_count: int) -> int:
        """Calculate search operation latency with parallel WL activation.

        Search operation activates multiple Word Lines simultaneously
        for Content Addressable Memory (CAM) functionality.

        The latency model:
        - Base sensing time equals tR (one read cycle)
        - Additional overhead for parallel WL coordination

        Note: Search uses LSB latency (simplified model, no multi-value consideration).

        Args:
            wl_count: Number of Word Lines to activate in parallel.

        Returns:
            Latency in nanoseconds.

        Raises:
            ValueError: If wl_count exceeds maximum or is invalid.
        """
        if wl_count <= 0:
            raise ValueError(f"Invalid WL count: {wl_count}. Must be positive.")
        if wl_count > self.parallel.max_parallel_wl:
            raise ValueError(
                f"WL count {wl_count} exceeds maximum {self.parallel.max_parallel_wl}"
            )

        # Search latency model:
        # - Parallel WL sensing takes base tR time (LSB)
        # - Overhead scales with log2 of WL count for comparison logic
        base_latency = self.timing.t_r_lsb
        # Parallel overhead: ~10% per doubling of WL count
        import math
        parallel_factor = 1.0 + 0.1 * math.log2(max(1, wl_count))
        return int(base_latency * parallel_factor)

    def get_compute_latency(self, block_count: int) -> int:
        """Calculate compute operation latency with parallel Block activation.

        Compute operation activates multiple Blocks for Multiply-Accumulate (MAC)
        functionality, accumulating current on Bit Lines.

        The latency model:
        - Base sensing time equals tR (one read cycle)
        - Additional overhead for multi-block current accumulation

        Note: Compute uses LSB latency (simplified model, no multi-value consideration).

        Args:
            block_count: Number of Blocks to activate in parallel.

        Returns:
            Latency in nanoseconds.

        Raises:
            ValueError: If block_count exceeds maximum or is invalid.
        """
        if block_count <= 0:
            raise ValueError(f"Invalid block count: {block_count}. Must be positive.")
        if block_count > self.parallel.max_parallel_blocks:
            raise ValueError(
                f"Block count {block_count} exceeds maximum {self.parallel.max_parallel_blocks}"
            )

        # Compute latency model:
        # - Parallel block sensing takes base tR time (LSB)
        # - MAC accumulation adds overhead proportional to block count
        base_latency = self.timing.t_r_lsb
        # Linear overhead for current accumulation on bit lines
        accumulation_factor = 1.0 + 0.15 * (block_count - 1)
        return int(base_latency * accumulation_factor)
