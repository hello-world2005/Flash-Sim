"""Flash simulator that executes command traces and returns latencies."""

from typing import Optional, List, Dict, Any
from .config import FlashConfig
from .chip import FlashChip


class CommandError(Exception):
    """Error raised when a command cannot be executed."""
    pass


class FlashSimulator:
    """Cycle-accurate flash simulator.

    Executes command sequences and returns latency for each operation.
    Supports read, write, erase, search, and compute operations.
    """

    SUPPORTED_COMMANDS = {"read", "write", "erase", "search", "compute"}

    def __init__(self, config: Optional[FlashConfig] = None):
        """Initialize simulator with configuration.

        Args:
            config: Flash configuration. Uses defaults if not provided.
        """
        self.config = config or FlashConfig()
        self.chip = FlashChip(self.config)

    def execute_command(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single command and return result with latency.

        Args:
            cmd: Command dictionary with 'type' field and operation-specific parameters.

        Returns:
            Result dictionary containing:
                - 'command': The original command type
                - 'latency_ns': Execution latency in nanoseconds
                - 'status': 'success' or 'error'
                - Additional fields based on command type

        Raises:
            CommandError: If command type is missing or unsupported.
        """
        if "type" not in cmd:
            raise CommandError("Missing required 'type' field in command")

        cmd_type = cmd["type"]
        if cmd_type not in self.SUPPORTED_COMMANDS:
            raise CommandError(f"Unsupported command type: {cmd_type}")

        handler = getattr(self, f"_execute_{cmd_type}")
        return handler(cmd)

    def _execute_read(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute read command."""
        address = cmd.get("address", 0)
        latency = self.chip.get_read_latency(address)
        return {
            "command": "read",
            "address": address,
            "latency_ns": latency,
            "status": "success",
        }

    def _execute_write(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute write command."""
        address = cmd.get("address", 0)
        latency = self.chip.get_write_latency(address)
        return {
            "command": "write",
            "address": address,
            "latency_ns": latency,
            "status": "success",
        }

    def _execute_erase(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute erase command."""
        block_address = cmd.get("block_address", cmd.get("address", 0))
        latency = self.chip.get_erase_latency(block_address)
        return {
            "command": "erase",
            "block_address": block_address,
            "latency_ns": latency,
            "status": "success",
        }

    def _execute_search(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute search command with parallel WL activation."""
        wl_count = cmd.get("wl_count", 1)
        try:
            latency = self.chip.get_search_latency(wl_count)
            return {
                "command": "search",
                "wl_count": wl_count,
                "latency_ns": latency,
                "status": "success",
            }
        except ValueError as e:
            raise CommandError(str(e))

    def _execute_compute(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Execute compute command with parallel Block activation."""
        block_count = cmd.get("block_count", 1)
        try:
            latency = self.chip.get_compute_latency(block_count)
            return {
                "command": "compute",
                "block_count": block_count,
                "latency_ns": latency,
                "status": "success",
            }
        except ValueError as e:
            raise CommandError(str(e))

    def run_trace(self, trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute a sequence of commands.

        Args:
            trace: List of command dictionaries.

        Returns:
            List of result dictionaries with latencies.
        """
        results = []
        for cmd in trace:
            try:
                result = self.execute_command(cmd)
                results.append(result)
            except CommandError as e:
                results.append({
                    "command": cmd.get("type", "unknown"),
                    "latency_ns": 0,
                    "status": "error",
                    "error": str(e),
                })
        return results

    def get_total_latency(self, results: List[Dict[str, Any]]) -> int:
        """Calculate total latency from execution results.

        Args:
            results: List of result dictionaries from run_trace.

        Returns:
            Total latency in nanoseconds.
        """
        return sum(r.get("latency_ns", 0) for r in results)
