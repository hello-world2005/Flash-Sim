"""Configuration classes for flash timing and parallelism parameters."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TimingConfig:
    """NAND Flash timing parameters in nanoseconds.

    Default values based on standard NAND Flash specifications.
    """
    t_r: int = 75_000        # Read latency (tR): 75 microseconds
    t_prog: int = 750_000    # Program latency (tPROG): 750 microseconds
    t_bers: int = 3_800_000  # Block erase latency (tBERS): 3.8 milliseconds

    def __post_init__(self):
        if self.t_r <= 0:
            raise ValueError("t_r must be positive")
        if self.t_prog <= 0:
            raise ValueError("t_prog must be positive")
        if self.t_bers <= 0:
            raise ValueError("t_bers must be positive")


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
class FlashConfig:
    """Complete flash simulator configuration."""
    timing: TimingConfig = field(default_factory=TimingConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)

    # Flash geometry (for address validation)
    pages_per_block: int = 256
    blocks_per_plane: int = 1024
    planes_per_die: int = 2

    @classmethod
    def from_dict(cls, config_dict: dict) -> "FlashConfig":
        """Create configuration from a dictionary."""
        timing_dict = config_dict.get("timing", {})
        parallel_dict = config_dict.get("parallel", {})

        timing = TimingConfig(
            t_r=timing_dict.get("t_r", 75_000),
            t_prog=timing_dict.get("t_prog", 750_000),
            t_bers=timing_dict.get("t_bers", 3_800_000),
        )

        parallel = ParallelConfig(
            max_parallel_wl=parallel_dict.get("max_parallel_wl", 64),
            max_parallel_blocks=parallel_dict.get("max_parallel_blocks", 8),
        )

        return cls(
            timing=timing,
            parallel=parallel,
            pages_per_block=config_dict.get("pages_per_block", 256),
            blocks_per_plane=config_dict.get("blocks_per_plane", 1024),
            planes_per_die=config_dict.get("planes_per_die", 2),
        )

    def to_dict(self) -> dict:
        """Convert configuration to a dictionary."""
        return {
            "timing": {
                "t_r": self.timing.t_r,
                "t_prog": self.timing.t_prog,
                "t_bers": self.timing.t_bers,
            },
            "parallel": {
                "max_parallel_wl": self.parallel.max_parallel_wl,
                "max_parallel_blocks": self.parallel.max_parallel_blocks,
            },
            "pages_per_block": self.pages_per_block,
            "blocks_per_plane": self.blocks_per_plane,
            "planes_per_die": self.planes_per_die,
        }
