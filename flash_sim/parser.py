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


# Command schema using LBA-based addressing
# Each command operates on LBA which maps to physical addresses via FTL
COMMAND_SCHEMA = {
    "read": {
        "required": ["time", "start_lha", "size"],
        "optional": ["bitmap"],  # address for backward compat
    },
    "write": {
        "required": ["time", "start_lha", "size"],
        "optional": ["data_address", "data_size", "invalidate", "bitmap"],  # user_erase oper is realized by invalidate, which will invalidate related page while write nothing
    },
    "static_write": {
        "required": ["time", "start_lha", "size"],
        "optional": ["data_address", "data_size", "invalidate"],  # user_erase oper is realized by invalidate, which will invalidate related page while write nothing
    },
    "search": {
        "required": ["time", "start_lha", "size", "data_address", "data_size"], # lha and size are in granularity of sub-plane
        "optional": ["bitmap", "wl_bitmap"],
    },
    "compute": {
        "required": ["time", "start_lha", "size", "data_address", "data_size"], # lha and size are in granularity of sub-plane
        "optional": ["bitmap", "wl_bitmap"],
    },
}


def validate_command(cmd: Dict[str, Any]) -> None:
    """Validate a single command against the schema.

    Args:
        cmd: Command dictionary to validate.

    Raises:
        ValidationError: If the command is invalid.
    """
    if not isinstance(cmd, dict):
        raise ValidationError(f"Command must be a dictionary, got {type(cmd).__name__}")

    if "type" not in cmd:
        raise ValidationError("Missing required 'type' field")

    cmd_type = cmd["type"]
    if cmd_type not in COMMAND_SCHEMA:
        raise ValidationError(f"Unknown command type: {cmd_type}")

    schema = COMMAND_SCHEMA[cmd_type]
    for field in schema["required"]:
        if field not in cmd:
            raise ValidationError(f"Missing required field '{field}' for {cmd_type} command")


def parse_trace(source: Union[str, Path, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Parse a command trace from various sources.

    Args:
        source: Can be:
            - A JSON string
            - A Path to a JSON file
            - A list of command dictionaries (passed through)

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
    for i, cmd in enumerate(commands):
        try:
            validate_command(cmd)
        except ValidationError as e:
            raise ValidationError(f"Invalid command at index {i}: {e}")

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
