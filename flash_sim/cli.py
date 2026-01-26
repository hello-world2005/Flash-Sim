"""Command-line interface for the flash simulator."""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .config import FlashConfig
from .simulator import FlashSimulator
from .parser import parse_trace, format_results, load_config, ParseError, ValidationError


def main(argv: Optional[list] = None) -> int:
    """Main entry point for CLI.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    parser = argparse.ArgumentParser(
        description="Cycle-accurate Flash Simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  flash-sim trace.json
  flash-sim trace.json --config config.json
  flash-sim trace.json --output results.json
  echo '[{"type": "read", "address": 0}]' | flash-sim -
        """
    )
    parser.add_argument(
        "trace",
        help="Path to JSON trace file, or '-' to read from stdin"
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to JSON configuration file"
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to output file (defaults to stdout)"
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Use compact JSON output"
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary statistics"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )

    args = parser.parse_args(argv)

    # Load configuration
    config = FlashConfig()
    if args.config:
        try:
            config_dict = load_config(args.config)
            config = FlashConfig.from_dict(config_dict)
            if args.verbose:
                print(f"Loaded config from {args.config}", file=sys.stderr)
        except ParseError as e:
            print(f"Error loading config: {e}", file=sys.stderr)
            return 1

    # Create simulator
    simulator = FlashSimulator(config)

    # Load trace
    try:
        if args.trace == "-":
            trace_content = sys.stdin.read()
            trace = parse_trace(trace_content)
        else:
            trace = parse_trace(Path(args.trace))
        if args.verbose:
            print(f"Loaded {len(trace)} commands", file=sys.stderr)
    except (ParseError, ValidationError) as e:
        print(f"Error loading trace: {e}", file=sys.stderr)
        return 1

    # Execute trace
    results = simulator.run_trace(trace)

    # Format output
    output = format_results(results, pretty=not args.compact)

    # Write output
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
            f.write("\n")
        if args.verbose:
            print(f"Results written to {args.output}", file=sys.stderr)
    else:
        print(output)

    # Print summary if requested
    if args.summary:
        total_latency = simulator.get_total_latency(results)
        success_count = sum(1 for r in results if r.get("status") == "success")
        error_count = len(results) - success_count
        print(f"\n--- Summary ---", file=sys.stderr)
        print(f"Total commands: {len(results)}", file=sys.stderr)
        print(f"Successful: {success_count}", file=sys.stderr)
        print(f"Errors: {error_count}", file=sys.stderr)
        print(f"Total latency: {total_latency:,} ns ({total_latency/1e6:.3f} ms)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
