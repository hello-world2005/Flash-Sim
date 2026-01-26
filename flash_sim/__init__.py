"""Cycle-accurate Flash Simulator supporting storage, search, and compute operations."""

from .config import TimingConfig, ParallelConfig, FlashConfig
from .chip import FlashChip
from .simulator import FlashSimulator
from .parser import parse_trace, validate_command

__version__ = "0.1.0"
__all__ = [
    "TimingConfig",
    "ParallelConfig",
    "FlashConfig",
    "FlashChip",
    "FlashSimulator",
    "parse_trace",
    "validate_command",
]
