"""Tests for JSON trace parser (AC-1)."""

import pytest
import json
import tempfile
from pathlib import Path

from flash_sim.parser import (
    parse_trace,
    validate_command,
    format_results,
    ParseError,
    ValidationError,
)


class TestParseTrace:
    """Tests for parse_trace function."""

    def test_valid_json_string_parsed(self):
        """Valid JSON command sequence parsed without errors."""
        trace_json = '[{"type": "read", "address": 0}, {"type": "write", "address": 1}]'
        commands = parse_trace(trace_json)
        assert len(commands) == 2
        assert commands[0]["type"] == "read"
        assert commands[1]["type"] == "write"

    def test_valid_json_file_parsed(self):
        """Valid JSON file is parsed correctly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([{"type": "read"}, {"type": "erase"}], f)
            f.flush()
            commands = parse_trace(Path(f.name))
        assert len(commands) == 2

    def test_required_fields_recognized(self):
        """Required fields (command type) recognized."""
        trace = '[{"type": "search", "lba": 0, "wl_count": 8}]'
        commands = parse_trace(trace)
        assert commands[0]["type"] == "search"
        assert commands[0]["lba"] == 0
        assert commands[0]["wl_count"] == 8

    def test_standalone_non_zero_address_preserved(self):
        """Standalone traces preserve non-zero logical addresses."""
        trace = '[{"type": "write", "lba": 1033, "data": 7}]'
        commands = parse_trace(trace, mode="standalone")
        assert commands[0]["lba"] == 1033
        assert commands[0]["data"] == 7

    def test_engine_trace_preserves_request_fields(self):
        """Engine traces keep time/start_lha/size payload intact."""
        trace = '[{"type": "read", "time": 5, "start_lha": 33, "size": 2}]'
        commands = parse_trace(trace)
        assert commands[0]["time"] == 5
        assert commands[0]["start_lha"] == 33
        assert commands[0]["size"] == 2

    def test_invalid_json_raises_parse_error(self):
        """Invalid JSON syntax raises ParseError."""
        with pytest.raises(ParseError, match="Invalid JSON"):
            parse_trace("{invalid json")

    def test_missing_type_raises_validation_error(self):
        """Missing required command type field raises validation error."""
        with pytest.raises(ValidationError, match="Missing required 'type' field"):
            parse_trace('[{"address": 0}]')

    def test_unknown_command_type_raises_error(self):
        """Unknown command type raises unsupported command error."""
        with pytest.raises(ValidationError, match="Unknown command type"):
            parse_trace('[{"type": "unknown_command"}]')

    def test_list_passthrough(self):
        """List of commands is passed through directly."""
        commands = [{"type": "read"}, {"type": "write"}]
        result = parse_trace(commands)
        assert result == commands

    def test_object_with_commands_field(self):
        """Object with 'commands' field is parsed correctly."""
        trace = '{"commands": [{"type": "read"}]}'
        commands = parse_trace(trace)
        assert len(commands) == 1
        assert commands[0]["type"] == "read"

    def test_object_with_trace_field(self):
        """Object with 'trace' field is parsed correctly."""
        trace = '{"trace": [{"type": "write"}]}'
        commands = parse_trace(trace)
        assert len(commands) == 1
        assert commands[0]["type"] == "write"

    def test_single_command_object(self):
        """Single command wrapped in object is handled."""
        trace = '{"type": "read", "address": 42}'
        commands = parse_trace(trace)
        assert len(commands) == 1
        assert commands[0]["type"] == "read"
        assert commands[0]["address"] == 42

    def test_file_not_found_raises_error(self):
        """Non-existent file raises ParseError."""
        with pytest.raises(ParseError, match="not found"):
            parse_trace(Path("/nonexistent/trace.json"))

    def test_mixed_schema_trace_raises_error(self):
        """Auto mode rejects traces that mix standalone and engine schemas."""
        trace = [
            {"type": "read", "lba": 1},
            {"type": "read", "time": 0, "start_lha": 1, "size": 1},
        ]
        with pytest.raises(ValidationError, match="mixes standalone and engine command schemas"):
            parse_trace(trace)


class TestValidateCommand:
    """Tests for validate_command function."""

    def test_valid_read_command(self):
        """Valid read command passes validation."""
        assert validate_command({"type": "read", "address": 0}) == "standalone"

    def test_valid_write_command(self):
        """Valid write command passes validation."""
        validate_command({"type": "write", "address": 0})

    def test_valid_erase_command(self):
        """Valid erase command passes validation."""
        validate_command({"type": "erase", "block_address": 0})

    def test_valid_search_command(self):
        """Valid search command passes validation."""
        validate_command({"type": "search", "lba": 0, "wl_count": 4})

    def test_valid_compute_command(self):
        """Valid compute command passes validation."""
        assert validate_command({"type": "compute", "lba": 0, "block_count": 2}) == "standalone"

    def test_engine_command_resolves_engine_mode(self):
        """Engine-style commands resolve to engine mode."""
        mode = validate_command(
            {"type": "write", "time": 0, "start_lha": 8, "size": 1}
        )
        assert mode == "engine"

    def test_non_dict_raises_error(self):
        """Non-dictionary command raises ValidationError."""
        with pytest.raises(ValidationError, match="must be a dictionary"):
            validate_command("not a dict")

    def test_missing_type_raises_error(self):
        """Missing type field raises ValidationError."""
        with pytest.raises(ValidationError, match="Missing required 'type' field"):
            validate_command({"address": 0})

    def test_standalone_mode_rejects_engine_command(self):
        """Standalone validation rejects engine-only request fields."""
        with pytest.raises(ValidationError, match="Engine-style traces use"):
            validate_command(
                {"type": "read", "time": 0, "start_lha": 1, "size": 1},
                mode="standalone",
            )


class TestFormatResults:
    """Tests for format_results function."""

    def test_pretty_format(self):
        """Pretty format includes indentation."""
        results = [{"command": "read", "latency_ns": 75000}]
        output = format_results(results, pretty=True)
        assert "\n" in output
        assert "  " in output

    def test_compact_format(self):
        """Compact format has no extra whitespace."""
        results = [{"command": "read", "latency_ns": 75000}]
        output = format_results(results, pretty=False)
        assert "\n" not in output
        assert "  " not in output

    def test_valid_json_output(self):
        """Output is valid JSON."""
        results = [{"command": "read", "latency_ns": 75000, "status": "success"}]
        output = format_results(results)
        parsed = json.loads(output)
        assert parsed == results
