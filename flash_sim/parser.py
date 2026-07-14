"""JSON trace parser and command validator."""

import json
from typing import List, Dict, Any, Union
from pathlib import Path


class ParseError(Exception):
    """Error raised when parsing fails."""
    pass


class ValidationError(Exception):
    """Error raised when command validation fails."""
    pass


# Standalone simulator schema used by FlashSimulator / `flash-sim run`.
STANDALONE_COMMAND_SCHEMA = {
    "read": {
        "required": [],
        "optional": ["lba", "address"],
    },
    "write": {
        "required": [],
        "optional": ["lba", "address", "data"],
    },
    "erase": {
        "required": [],
        "optional": ["lba", "address", "block_address"],
    },
    "search": {
        "required": ["wl_count"],
        "optional": ["lba", "address"],
    },
    "compute": {
        "required": ["block_count"],
        "optional": ["lba", "address", "layer"],
    },
}

# Event-driven engine schema used by Engine / `flash-sim run-engine`.
ENGINE_COMMAND_SCHEMA = {
    "read": {
        "required": ["time", "start_lha", "size"],
        "optional": ["bitmap", "stream_id"],
    },
    "write": {
        "required": ["time", "start_lha", "size"],
        "optional": ["invalidate", "bitmap", "stream_id"],
    },
    "static_write": {
        "required": ["time", "start_lha", "size"],
        "optional": ["invalidate", "stream_id"],
    },
    "search": {
        "required": ["time", "start_lha", "size"],
        "optional": ["bitmap", "wl_bitmap", "stream_id", "data_address", "data_size"],
    },
    "compute": {
        "required": ["time", "start_lha", "size", "selected_wl"],
        "optional": ["bitmap", "wl_bitmap", "stream_id", "data_address", "data_size"],
    },
}

ENGINE_FIELDS = {"time", "start_lha", "size"}
STANDALONE_HINT_FIELDS = {"lba", "address", "block_address", "wl_count", "block_count", "layer"}


def _looks_like_engine_command(cmd: Dict[str, Any]) -> bool:
    return any(field in cmd for field in ENGINE_FIELDS)


def _looks_like_standalone_command(cmd: Dict[str, Any]) -> bool:
    return any(field in cmd for field in STANDALONE_HINT_FIELDS)


def _resolve_mode(cmd: Dict[str, Any], mode: str) -> str:
    if mode not in {"auto", "standalone", "engine"}:
        raise ValidationError(f"Unknown trace mode: {mode}")

    if mode == "standalone":
        if _looks_like_engine_command(cmd):
            raise ValidationError(
                "Engine-style traces use 'time', 'start_lha', and 'size'; run them via the event-driven engine path"
            )
        return "standalone"

    if mode == "engine":
        if not _looks_like_engine_command(cmd):
            raise ValidationError(
                "Engine trace commands must provide 'time', 'start_lha', and 'size'"
            )
        if _looks_like_standalone_command(cmd):
            raise ValidationError("Command mixes standalone and engine trace fields")
        return "engine"

    if _looks_like_engine_command(cmd):
        if _looks_like_standalone_command(cmd):
            raise ValidationError("Command mixes standalone and engine trace fields")
        return "engine"
    return "standalone"


def validate_command(cmd: Dict[str, Any], mode: str = "auto") -> str:
    """Validate a single command against the schema.

    Args:
        cmd: Command dictionary to validate.
        mode: One of `auto`, `standalone`, or `engine`.

    Raises:
        ValidationError: If the command is invalid.
    """
    if not isinstance(cmd, dict):
        raise ValidationError(f"Command must be a dictionary, got {type(cmd).__name__}")

    if "type" not in cmd:
        raise ValidationError("Missing required 'type' field")

    resolved_mode = _resolve_mode(cmd, mode)
    command_schema = (
        ENGINE_COMMAND_SCHEMA if resolved_mode == "engine" else STANDALONE_COMMAND_SCHEMA
    )

    cmd_type = cmd["type"]
    if cmd_type not in command_schema:
        raise ValidationError(f"Unknown command type: {cmd_type}")

    schema = command_schema[cmd_type]
    for field in schema["required"]:
        if field not in cmd:
            raise ValidationError(f"Missing required field '{field}' for {cmd_type} command")
    if resolved_mode == "engine" and cmd_type == "compute":
        selected_wl = cmd["selected_wl"]
        if isinstance(selected_wl, bool) or not isinstance(selected_wl, int):
            raise ValidationError("Field 'selected_wl' for compute command must be an integer")
    return resolved_mode


def parse_trace(
    source: Union[str, Path, List[Dict[str, Any]]],
    mode: str = "auto",
) -> List[Dict[str, Any]]:
    """Parse a command trace from various sources.

    Args:
        source: Can be:
            - A JSON string
            - A Path to a JSON file
            - A list of command dictionaries (passed through)
        mode: One of `auto`, `standalone`, or `engine`.

    Returns:
        List of command dictionaries.

    Raises:
        ParseError: If parsing fails.
        ValidationError: If any command is invalid.
    """
    if isinstance(source, list):
        commands = source
    elif isinstance(source, Path):
        try:
            with open(source, "r") as f:
                content = f.read()
        except FileNotFoundError:
            raise ParseError(f"Trace file not found: {source}")
        except IOError as e:
            raise ParseError(f"Error reading trace file: {e}")
        commands = _parse_json(content)
    elif isinstance(source, str):
        # Check if it's a file path
        if source.endswith(".json") and not source.strip().startswith("["):
            return parse_trace(Path(source))
        commands = _parse_json(source)
    else:
        raise ParseError(f"Invalid source type: {type(source).__name__}")

    # Validate all commands
    resolved_modes = set()
    for i, cmd in enumerate(commands):
        try:
            resolved_modes.add(validate_command(cmd, mode=mode))
        except ValidationError as e:
            raise ValidationError(f"Invalid command at index {i}: {e}")

    if mode == "auto" and len(resolved_modes) > 1:
        raise ValidationError("Trace mixes standalone and engine command schemas")

    return commands


def _parse_json(content: str) -> List[Dict[str, Any]]:
    """Parse JSON content into command list.

    Args:
        content: JSON string.

    Returns:
        List of command dictionaries.

    Raises:
        ParseError: If JSON parsing fails.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON: {e}")

    # Handle both array of commands and object with 'commands' field
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        if "commands" in data:
            return data["commands"]
        elif "trace" in data:
            return data["trace"]
        else:
            # Single command wrapped in object
            return [data]
    else:
        raise ParseError(f"Expected array or object, got {type(data).__name__}")


def format_results(results: List[Dict[str, Any]], pretty: bool = True) -> str:
    """Format execution results as JSON string.

    Args:
        results: List of result dictionaries.
        pretty: Whether to use pretty formatting.

    Returns:
        JSON string.
    """
    indent = 2 if pretty else None
    return json.dumps(results, indent=indent)


def load_config(source: Union[str, Path]) -> Dict[str, Any]:
    """Load configuration from JSON file or string.

    Args:
        source: JSON string or path to JSON file.

    Returns:
        Configuration dictionary.

    Raises:
        ParseError: If parsing fails.
    """
    if isinstance(source, Path) or (isinstance(source, str) and source.endswith(".json")):
        path = Path(source)
        try:
            with open(path, "r") as f:
                content = f.read()
        except FileNotFoundError:
            raise ParseError(f"Config file not found: {path}")
        except IOError as e:
            raise ParseError(f"Error reading config file: {e}")
    else:
        content = source

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ParseError(f"Invalid JSON config: {e}")
