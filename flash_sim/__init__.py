"""Cycle-accurate Flash Simulator supporting storage, search, and compute operations."""

from .config import (
    TimingConfig, OnfiTimingConfig, ParallelConfig, FlashConfig, FlashGeometry, FlashAddress,
    PageStatus, BlockStatus, PageInfo, BlockInfo, FTL
)
from .chip import FlashChip
from .simulator import FlashSimulator
from .parser import parse_trace, validate_command

__version__ = "0.2.0"
__all__ = [
    "TimingConfig",
    "OnfiTimingConfig",
    "ParallelConfig",
    "FlashConfig",
    "FlashGeometry",
    "FlashAddress",
    "PageStatus",
    "BlockStatus",
    "PageInfo",
    "BlockInfo",
    "FTL",
    "FlashChip",
    "FlashSimulator",
    "parse_trace",
    "validate_command",
]
