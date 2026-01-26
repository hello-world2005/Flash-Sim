"""Tests for configuration classes."""

import pytest
from flash_sim.config import TimingConfig, ParallelConfig, FlashConfig


class TestTimingConfig:
    """Tests for TimingConfig."""

    def test_default_values(self):
        """Default timing values match standard NAND Flash specs."""
        config = TimingConfig()
        assert config.t_r == 75_000  # 75us
        assert config.t_prog == 750_000  # 750us
        assert config.t_bers == 3_800_000  # 3.8ms

    def test_custom_values(self):
        """Custom timing values are accepted."""
        config = TimingConfig(t_r=100_000, t_prog=500_000, t_bers=2_000_000)
        assert config.t_r == 100_000
        assert config.t_prog == 500_000
        assert config.t_bers == 2_000_000

    def test_invalid_t_r_raises_error(self):
        """Zero or negative t_r raises ValueError."""
        with pytest.raises(ValueError, match="t_r must be positive"):
            TimingConfig(t_r=0)
        with pytest.raises(ValueError, match="t_r must be positive"):
            TimingConfig(t_r=-1)

    def test_invalid_t_prog_raises_error(self):
        """Zero or negative t_prog raises ValueError."""
        with pytest.raises(ValueError, match="t_prog must be positive"):
            TimingConfig(t_prog=0)

    def test_invalid_t_bers_raises_error(self):
        """Zero or negative t_bers raises ValueError."""
        with pytest.raises(ValueError, match="t_bers must be positive"):
            TimingConfig(t_bers=0)


class TestParallelConfig:
    """Tests for ParallelConfig."""

    def test_default_values(self):
        """Default parallel values are reasonable."""
        config = ParallelConfig()
        assert config.max_parallel_wl == 64
        assert config.max_parallel_blocks == 8

    def test_custom_values(self):
        """Custom parallel values are accepted."""
        config = ParallelConfig(max_parallel_wl=128, max_parallel_blocks=16)
        assert config.max_parallel_wl == 128
        assert config.max_parallel_blocks == 16

    def test_invalid_wl_count_raises_error(self):
        """Zero or negative max_parallel_wl raises ValueError."""
        with pytest.raises(ValueError, match="max_parallel_wl must be positive"):
            ParallelConfig(max_parallel_wl=0)

    def test_invalid_block_count_raises_error(self):
        """Zero or negative max_parallel_blocks raises ValueError."""
        with pytest.raises(ValueError, match="max_parallel_blocks must be positive"):
            ParallelConfig(max_parallel_blocks=0)


class TestFlashConfig:
    """Tests for FlashConfig."""

    def test_default_config(self):
        """Default config has expected values."""
        config = FlashConfig()
        assert isinstance(config.timing, TimingConfig)
        assert isinstance(config.parallel, ParallelConfig)
        assert config.pages_per_block == 256
        assert config.blocks_per_plane == 1024

    def test_from_dict(self):
        """Config can be created from dictionary."""
        config_dict = {
            "timing": {"t_r": 100_000, "t_prog": 600_000},
            "parallel": {"max_parallel_wl": 32},
            "pages_per_block": 128,
        }
        config = FlashConfig.from_dict(config_dict)
        assert config.timing.t_r == 100_000
        assert config.timing.t_prog == 600_000
        assert config.timing.t_bers == 3_800_000  # default
        assert config.parallel.max_parallel_wl == 32
        assert config.parallel.max_parallel_blocks == 8  # default
        assert config.pages_per_block == 128

    def test_to_dict(self):
        """Config can be serialized to dictionary."""
        config = FlashConfig()
        config_dict = config.to_dict()
        assert config_dict["timing"]["t_r"] == 75_000
        assert config_dict["parallel"]["max_parallel_wl"] == 64
        assert config_dict["pages_per_block"] == 256

    def test_from_dict_empty(self):
        """Empty dict produces default config."""
        config = FlashConfig.from_dict({})
        assert config.timing.t_r == 75_000
        assert config.parallel.max_parallel_wl == 64
