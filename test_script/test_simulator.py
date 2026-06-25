"""Tests for FlashSimulator class."""

import pytest
from flash_sim.simulator import FlashSimulator, CommandError
from flash_sim.config import FlashConfig, TimingConfig, ParallelConfig


class TestSimulatorBasic:
    """Basic simulator tests."""

    def test_default_config(self):
        """Simulator initializes with default config."""
        sim = FlashSimulator()
        assert sim.config is not None
        assert sim.chip is not None

    def test_custom_config(self):
        """Simulator accepts custom configuration."""
        config = FlashConfig(timing=TimingConfig(t_r_lsb=100_000))
        sim = FlashSimulator(config)
        assert sim.config.timing.t_r_lsb == 100_000


class TestExecuteCommand:
    """Tests for execute_command method."""

    def test_missing_type_raises_error(self):
        """Missing command type raises CommandError."""
        sim = FlashSimulator()
        with pytest.raises(CommandError, match="Missing required 'type' field"):
            sim.execute_command({"address": 0})

    def test_unsupported_type_raises_error(self):
        """Unsupported command type raises CommandError."""
        sim = FlashSimulator()
        with pytest.raises(CommandError, match="Unsupported command type"):
            sim.execute_command({"type": "invalid"})

    def test_read_command(self):
        """Read command executes correctly."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "read", "address": 100})
        assert result["command"] == "read"
        assert result["address"] == 100
        assert result["latency_ns"] == sim.config.timing.t_r_lsb
        assert result["status"] == "success"

    def test_write_command(self):
        """Write command executes correctly."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "write", "address": 200})
        assert result["command"] == "write"
        assert result["address"] == 200
        assert result["latency_ns"] == sim.config.timing.t_prog_lsb
        assert result["status"] == "success"

    def test_erase_command(self):
        """Erase command executes correctly."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "erase", "block_address": 10})
        assert result["command"] == "erase"
        assert result["block_address"] == 10
        assert result["latency_ns"] == sim.config.timing.t_bers
        assert result["status"] == "success"

    def test_erase_with_address_field(self):
        """Erase command accepts address field as fallback."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "erase", "address": 5})
        assert result["block_address"] == 5

    def test_search_command(self):
        """Search command executes correctly."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "search", "wl_count": 8})
        assert result["command"] == "search"
        assert result["wl_count"] == 8
        assert result["latency_ns"] > 0
        assert result["status"] == "success"

    def test_search_invalid_wl_raises_error(self):
        """Search with invalid WL count raises CommandError."""
        sim = FlashSimulator()
        with pytest.raises(CommandError, match="Invalid WL count"):
            sim.execute_command({"type": "search", "wl_count": 0})

    def test_compute_command(self):
        """Compute command executes correctly."""
        sim = FlashSimulator()
        result = sim.execute_command({"type": "compute", "block_count": 4})
        assert result["command"] == "compute"
        assert result["block_count"] == 4
        assert result["latency_ns"] > 0
        assert result["status"] == "success"

    def test_compute_invalid_blocks_raises_error(self):
        """Compute with invalid block count raises CommandError."""
        sim = FlashSimulator()
        with pytest.raises(CommandError, match="Invalid block count"):
            sim.execute_command({"type": "compute", "block_count": -1})


class TestRunTrace:
    """Tests for run_trace method."""

    def test_empty_trace(self):
        """Empty trace returns empty results."""
        sim = FlashSimulator()
        results = sim.run_trace([])
        assert results == []

    def test_single_command(self):
        """Single command trace works correctly."""
        sim = FlashSimulator()
        results = sim.run_trace([{"type": "read"}])
        assert len(results) == 1
        assert results[0]["command"] == "read"

    def test_multiple_commands(self):
        """Multiple command trace executes all commands."""
        sim = FlashSimulator()
        trace = [
            {"type": "read", "address": 0},
            {"type": "write", "address": 1},
            {"type": "erase", "block_address": 0},
        ]
        results = sim.run_trace(trace)
        assert len(results) == 3
        assert results[0]["command"] == "read"
        assert results[1]["command"] == "write"
        assert results[2]["command"] == "erase"

    def test_error_handling_in_trace(self):
        """Errors in trace don't stop execution."""
        sim = FlashSimulator()
        trace = [
            {"type": "read"},
            {"type": "invalid"},
            {"type": "write"},
        ]
        results = sim.run_trace(trace)
        assert len(results) == 3
        assert results[0]["status"] == "success"
        assert results[1]["status"] == "error"
        assert results[2]["status"] == "success"


class TestTotalLatency:
    """Tests for get_total_latency method."""

    def test_total_latency_calculation(self):
        """Total latency is sum of individual latencies."""
        sim = FlashSimulator()
        trace = [
            {"type": "read"},   # 75,000 ns
            {"type": "write"},  # 750,000 ns
            {"type": "erase"},  # 3,800,000 ns
        ]
        results = sim.run_trace(trace)
        total = sim.get_total_latency(results)
        assert total == (
            sim.config.timing.t_r_lsb
            + sim.config.timing.t_prog_lsb
            + sim.config.timing.t_bers
        )

    def test_total_latency_ignores_errors(self):
        """Total latency calculation handles errors (0 latency)."""
        sim = FlashSimulator()
        trace = [
            {"type": "read"},
            {"type": "invalid"},
            {"type": "read"},
        ]
        results = sim.run_trace(trace)
        total = sim.get_total_latency(results)
        assert total == sim.config.timing.t_r_lsb * 2


class TestConfiguredLatencies:
    """Tests for configured timing parameters."""

    def test_configured_read_latency(self):
        """Read latency reflects configuration."""
        config = FlashConfig(timing=TimingConfig(t_r_lsb=50_000))
        sim = FlashSimulator(config)
        result = sim.execute_command({"type": "read"})
        assert result["latency_ns"] == 50_000

    def test_configured_write_latency(self):
        """Write latency reflects configuration."""
        config = FlashConfig(timing=TimingConfig(t_prog_lsb=500_000))
        sim = FlashSimulator(config)
        result = sim.execute_command({"type": "write"})
        assert result["latency_ns"] == 500_000

    def test_configured_erase_latency(self):
        """Erase latency reflects configuration."""
        config = FlashConfig(timing=TimingConfig(t_bers=5_000_000))
        sim = FlashSimulator(config)
        result = sim.execute_command({"type": "erase"})
        assert result["latency_ns"] == 5_000_000
