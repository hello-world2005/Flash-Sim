"""Tests for FlashChip class."""

import pytest
from flash_sim.chip import FlashChip
from flash_sim.config import FlashConfig, TimingConfig, ParallelConfig


class TestReadLatency:
    """Tests for read operation latency (AC-2)."""

    def test_read_returns_t_r(self):
        """Read command returns latency equal to tR timing parameter."""
        chip = FlashChip()
        latency = chip.get_read_latency(address=0)
        assert latency == chip.timing.t_r
        assert latency == 75_000  # default tR

    def test_read_consistent_across_addresses(self):
        """Latency is consistent across multiple reads to different addresses."""
        chip = FlashChip()
        latencies = [chip.get_read_latency(addr) for addr in [0, 100, 1000, 10000]]
        assert all(lat == latencies[0] for lat in latencies)

    def test_read_respects_configured_timing(self):
        """Configured timing parameters affect the returned latency."""
        config = FlashConfig(timing=TimingConfig(t_r=100_000))
        chip = FlashChip(config)
        assert chip.get_read_latency(0) == 100_000

    def test_read_latency_is_positive(self):
        """Read operation never returns zero or negative latency."""
        chip = FlashChip()
        latency = chip.get_read_latency(0)
        assert latency > 0

    def test_read_differs_from_write_and_erase(self):
        """Read latency differs from write and erase latencies."""
        chip = FlashChip()
        read_lat = chip.get_read_latency(0)
        write_lat = chip.get_write_latency(0)
        erase_lat = chip.get_erase_latency(0)
        assert read_lat != write_lat
        assert read_lat != erase_lat


class TestWriteLatency:
    """Tests for write operation latency (AC-3)."""

    def test_write_returns_t_prog(self):
        """Write command returns latency equal to tPROG timing parameter."""
        chip = FlashChip()
        latency = chip.get_write_latency(address=0)
        assert latency == chip.timing.t_prog
        assert latency == 750_000  # default tPROG

    def test_write_differs_from_read_and_erase(self):
        """Write latency differs from read and erase latencies."""
        chip = FlashChip()
        read_lat = chip.get_read_latency(0)
        write_lat = chip.get_write_latency(0)
        erase_lat = chip.get_erase_latency(0)
        assert write_lat != read_lat
        assert write_lat != erase_lat

    def test_write_respects_configured_timing(self):
        """Configured timing parameters affect write latency."""
        config = FlashConfig(timing=TimingConfig(t_prog=500_000))
        chip = FlashChip(config)
        assert chip.get_write_latency(0) == 500_000

    def test_write_latency_above_minimum(self):
        """Write latency is above minimum program time threshold."""
        chip = FlashChip()
        latency = chip.get_write_latency(0)
        # Write should be significantly longer than read
        assert latency > chip.timing.t_r


class TestEraseLatency:
    """Tests for erase operation latency (AC-4)."""

    def test_erase_returns_t_bers(self):
        """Erase command returns latency equal to tBERS timing parameter."""
        chip = FlashChip()
        latency = chip.get_erase_latency(block_address=0)
        assert latency == chip.timing.t_bers
        assert latency == 3_800_000  # default tBERS

    def test_erase_is_longest_operation(self):
        """Erase latency is the longest among read/write/erase operations."""
        chip = FlashChip()
        read_lat = chip.get_read_latency(0)
        write_lat = chip.get_write_latency(0)
        erase_lat = chip.get_erase_latency(0)
        assert erase_lat > read_lat
        assert erase_lat > write_lat

    def test_erase_respects_configured_timing(self):
        """Configured timing parameters affect erase latency."""
        config = FlashConfig(timing=TimingConfig(t_bers=5_000_000))
        chip = FlashChip(config)
        assert chip.get_erase_latency(0) == 5_000_000

    def test_erase_latency_above_minimum(self):
        """Erase latency is above minimum erase time threshold."""
        chip = FlashChip()
        latency = chip.get_erase_latency(0)
        # Erase should be significantly longer than write
        assert latency > chip.timing.t_prog


class TestSearchLatency:
    """Tests for search operation latency (AC-5)."""

    def test_search_returns_latency(self):
        """Search command returns latency for parallel WL activation."""
        chip = FlashChip()
        latency = chip.get_search_latency(wl_count=1)
        assert latency > 0

    def test_search_parallel_wl_affects_latency(self):
        """Parallel WL count affects latency."""
        chip = FlashChip()
        lat_1 = chip.get_search_latency(wl_count=1)
        lat_32 = chip.get_search_latency(wl_count=32)
        lat_64 = chip.get_search_latency(wl_count=64)
        # More WLs should have higher latency due to overhead
        assert lat_32 > lat_1
        assert lat_64 > lat_32

    def test_search_based_on_read_sensing(self):
        """Search latency is based on read-like sensing operation."""
        chip = FlashChip()
        search_lat = chip.get_search_latency(wl_count=1)
        read_lat = chip.get_read_latency(0)
        # Single WL search should be close to read latency
        assert search_lat >= read_lat
        assert search_lat < read_lat * 2

    def test_search_invalid_wl_count_raises_error(self):
        """Search with invalid WL count raises error."""
        chip = FlashChip()
        with pytest.raises(ValueError, match="Invalid WL count"):
            chip.get_search_latency(wl_count=0)
        with pytest.raises(ValueError, match="Invalid WL count"):
            chip.get_search_latency(wl_count=-1)

    def test_search_exceeds_max_wl_raises_error(self):
        """Search exceeding max WL count raises error."""
        config = FlashConfig(parallel=ParallelConfig(max_parallel_wl=32))
        chip = FlashChip(config)
        with pytest.raises(ValueError, match="exceeds maximum"):
            chip.get_search_latency(wl_count=64)


class TestComputeLatency:
    """Tests for compute operation latency (AC-6)."""

    def test_compute_returns_latency(self):
        """Compute command returns latency for parallel Block activation."""
        chip = FlashChip()
        latency = chip.get_compute_latency(block_count=1)
        assert latency > 0

    def test_compute_parallel_blocks_affects_latency(self):
        """Parallel Block count affects latency."""
        chip = FlashChip()
        lat_1 = chip.get_compute_latency(block_count=1)
        lat_4 = chip.get_compute_latency(block_count=4)
        lat_8 = chip.get_compute_latency(block_count=8)
        # More blocks should have higher latency due to MAC accumulation
        assert lat_4 > lat_1
        assert lat_8 > lat_4

    def test_compute_based_on_parallel_sensing(self):
        """Compute latency based on parallel bit-line sensing."""
        chip = FlashChip()
        compute_lat = chip.get_compute_latency(block_count=1)
        read_lat = chip.get_read_latency(0)
        # Single block compute should be close to read latency
        assert compute_lat >= read_lat
        assert compute_lat < read_lat * 2

    def test_compute_invalid_block_count_raises_error(self):
        """Compute with invalid Block count raises error."""
        chip = FlashChip()
        with pytest.raises(ValueError, match="Invalid block count"):
            chip.get_compute_latency(block_count=0)
        with pytest.raises(ValueError, match="Invalid block count"):
            chip.get_compute_latency(block_count=-1)

    def test_compute_exceeds_max_blocks_raises_error(self):
        """Compute exceeding max block count raises error."""
        config = FlashConfig(parallel=ParallelConfig(max_parallel_blocks=4))
        chip = FlashChip(config)
        with pytest.raises(ValueError, match="exceeds maximum"):
            chip.get_compute_latency(block_count=8)
