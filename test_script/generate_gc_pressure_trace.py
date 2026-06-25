"""Generate a write-intensive trace targeting a single plane to trigger GC.

This script produces a Flash-Sim engine trace that writes enough pages to a
single dynamic plane to exhaust its free-block pool and force GC cycles.
No preconditioning data is required — the simulator starts with an empty state.

Usage::

    python test_script/generate_gc_pressure_trace.py --output test_case/gc_pressure_trace.json

Default parameters produce ~860 requests (~91 % writes, ~9 % reads) and are
expected to trigger 2-3 GC cycles on the target plane.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow running the script directly from the repo root and importing it from tests.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if __package__ in (None, ""):
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from flash_sim.common import (
    BLOCK_PER_PLANE,
    CHANNEL_NO,
    CHIP_PER_CHANNEL,
    DIE_PER_CHIP,
    GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
    LPA_NO_PER_MAPPING_PAGE,
    PAGE_PER_BLOCK,
    PLANE_PER_DIE,
    SECTOR_PER_PAGE,
    STATIC_CHIP_PER_CHANNEL,
)

# ---------------------------------------------------------------------------
# Derived geometry
# ---------------------------------------------------------------------------

_PAGES_PER_PLANE = BLOCK_PER_PLANE * PAGE_PER_BLOCK  # 512
_NON_STATIC_CHIPS = CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL  # 3

_TOTAL_RANDOM_ACCESS_PAGES = (
    CHANNEL_NO * _NON_STATIC_CHIPS * DIE_PER_CHIP * PLANE_PER_DIE * _PAGES_PER_PLANE
)  # 196_608

_MAPPING_PAGE_COUNT = math.ceil(_TOTAL_RANDOM_ACCESS_PAGES / LPA_NO_PER_MAPPING_PAGE)  # 768

RANDOM_ACCESS_DATA_PAGES = _TOTAL_RANDOM_ACCESS_PAGES - _MAPPING_PAGE_COUNT  # 195_840

# Writes needed to burn through blocks until GC threshold is crossed:
#   (BLOCK_PER_PLANE - threshold - 1) * PAGE_PER_BLOCK + 1
# = (64 - 3 - 1) * 8 + 1 = 481
MIN_WRITES_FOR_GC = (BLOCK_PER_PLANE - GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD - 1) * PAGE_PER_BLOCK + 1

DEFAULT_OUTPUT_PATH = _REPO_ROOT / "test_case" / "gc_pressure_trace.json"
TIME_STEP_NS = 10
# Larger default for write-intensive traces so GC has time to recycle blocks
# between batches.  tPROG ≈ 250 µs, GC_ERASE ≈ 10 ms.
SAFE_TIME_STEP_NS = 500_000   # 500 µs — longer than tPROG, gives GC breathing room


# ---------------------------------------------------------------------------
# LPA <-> plane mapping (pure math, matches AMU.get_plane_address_for_lpa)
# ---------------------------------------------------------------------------

def plane_key_for_lpa(lpa: int) -> tuple[int, int, int, int]:
    """Return (channel, chip, die, plane) for *lpa*.

    The logic mirrors ``Address_Mapping_Unit.get_plane_address_for_lpa``
    so that trace LPAs route to the intended physical plane at runtime.
    """
    if lpa < 0 or lpa >= RANDOM_ACCESS_DATA_PAGES:
        raise ValueError(f"LPA {lpa} out of range [0, {RANDOM_ACCESS_DATA_PAGES})")

    plane_value = lpa // _PAGES_PER_PLANE
    plane = plane_value % PLANE_PER_DIE
    plane_value //= PLANE_PER_DIE
    die = plane_value % DIE_PER_CHIP
    plane_value //= DIE_PER_CHIP
    chip = plane_value % CHIP_PER_CHANNEL
    channel = plane_value // CHIP_PER_CHANNEL
    return (channel, chip, die, plane)


def enumerate_lpas_for_plane(
    target: tuple[int, int, int, int],
    max_lpa: int = RANDOM_ACCESS_DATA_PAGES,
) -> list[int]:
    """Return every LPA in ``[0, max_lpa)`` that maps to *target*."""
    return [lpa for lpa in range(max_lpa) if plane_key_for_lpa(lpa) == target]


# ---------------------------------------------------------------------------
# Phase builders
# ---------------------------------------------------------------------------

def _make_write(lpa: int, time_ns: int) -> dict[str, Any]:
    return {
        "type": "write",
        "time": time_ns,
        "start_lha": lpa * SECTOR_PER_PAGE,
        "size": SECTOR_PER_PAGE,
    }


def _make_read(lpa: int, time_ns: int, offset: int = 0, size: int | None = None) -> dict[str, Any]:
    if size is None:
        size = SECTOR_PER_PAGE
    return {
        "type": "read",
        "time": time_ns,
        "start_lha": lpa * SECTOR_PER_PAGE + offset,
        "size": size,
    }


def build_fill_phase(
    lpas: list[int],
    count: int,
    start_time: int = 0,
    time_step: int = TIME_STEP_NS,
) -> list[dict[str, Any]]:
    """Sequential full-page writes to the first *count* LPAs."""
    cmds: list[dict[str, Any]] = []
    for i, lpa in enumerate(lpas[:count]):
        cmds.append(_make_write(lpa, start_time + i * time_step))
    return cmds


def build_overwrite_phase(
    lpas: list[int],
    overwrite_targets: list[int],
    start_time: int,
    time_step: int = TIME_STEP_NS,
) -> list[dict[str, Any]]:
    """Re-write *overwrite_targets* to create invalid pages in old blocks."""
    cmds: list[dict[str, Any]] = []
    t = start_time
    for lpa in overwrite_targets:
        cmds.append(_make_write(lpa, t))
        t += time_step
    return cmds


def build_sustained_phase(
    available_lpas: list[int],
    written_lpas: set[int],
    rng,
    start_time: int,
    time_step: int = TIME_STEP_NS,
    num_cycles: int = 30,
    writes_per_cycle: int = 8,
    reads_per_cycle: int = 2,
    max_unique_lpas: int | None = None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Alternate write batches with read batches.

    Writes prefer already-written LPAs to create invalid pages for GC.
    New LPAs are only used when the unique LPA cap allows it.
    """
    cmds: list[dict[str, Any]] = []
    t = start_time
    lpa_capacity_remaining = (
        (max_unique_lpas - len(written_lpas)) if max_unique_lpas else float("inf")
    )

    for _ in range(num_cycles):
        # -- write batch --
        for _ in range(writes_per_cycle):
            # Strongly prefer overwriting existing LPAs (creates invalid pages,
            # keeps unique LPA count stable).  Only use a fresh LPA when we
            # have explicit headroom.
            can_use_fresh = lpa_capacity_remaining > 0
            if written_lpas and (not can_use_fresh or rng.random() < 0.85):
                lpa = rng.choice(list(written_lpas))
            else:
                lpa = rng.choice(available_lpas)
                if lpa not in written_lpas:
                    lpa_capacity_remaining -= 1
            cmds.append(_make_write(lpa, t))
            written_lpas.add(lpa)
            t += time_step

        # -- read batch --
        read_candidates = list(written_lpas)
        for _ in range(reads_per_cycle):
            if not read_candidates:
                break
            lpa = rng.choice(read_candidates)
            size = rng.randint(1, SECTOR_PER_PAGE)
            offset = rng.randint(0, SECTOR_PER_PAGE - size)
            cmds.append(_make_read(lpa, t, offset=offset, size=size))
            t += time_step

    return cmds, written_lpas


def build_final_read_phase(
    written_lpas: list[int],
    rng,
    count: int,
    start_time: int,
    time_step: int = TIME_STEP_NS,
) -> list[dict[str, Any]]:
    """Read back a random sample of previously written LPAs."""
    cmds: list[dict[str, Any]] = []
    chosen = rng.sample(written_lpas, min(count, len(written_lpas)))
    t = start_time
    for lpa in chosen:
        cmds.append(_make_read(lpa, t))
        t += time_step
    return cmds


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def generate_trace(
    *,
    seed: int = 0,
    target_plane: tuple[int, int, int, int] = (0, 0, 0, 0),
    fill_count: int | None = None,
    overwrite_count: int = 64,
    sustained_cycles: int = 30,
    writes_per_cycle: int = 8,
    reads_per_cycle: int = 2,
    final_reads: int = 16,
    time_step_ns: int = TIME_STEP_NS,
) -> list[dict[str, Any]]:
    """Generate a write-intensive GC-pressure trace.

    Parameters
    ----------
    seed:
        Random seed for reproducibility.
    target_plane:
        ``(channel, chip, die, plane)`` — chip must be non-static (0-2).
    fill_count:
        Number of initial fill writes.  ``None`` (default) selects a value
        that stops just above the GC threshold.
    overwrite_count:
        Overwrites to create invalid pages.  Must be ≤ *fill_count*.
    sustained_cycles:
        Number of (write-batch + read-batch) iterations.
    writes_per_cycle / reads_per_cycle:
        Requests per batch in the sustained phase.
    final_reads:
        Read-back count for the final verification phase.
    time_step_ns:
        Nanoseconds between consecutive requests.
    """
    # --- validate target ---
    channel, chip, die, plane = target_plane
    if chip >= _NON_STATIC_CHIPS:
        raise ValueError(
            f"Target chip {chip} is static; only non-static chips [0, {_NON_STATIC_CHIPS - 1}] "
            f"accept user read/write traffic."
        )

    # --- resolve LPAs ---
    available_lpas = enumerate_lpas_for_plane(target_plane)
    plane_capacity = len(available_lpas)
    if plane_capacity < 400:
        raise ValueError(
            f"Plane {target_plane} has only {plane_capacity} LPAs; "
            f"need ≥ 400 for a meaningful GC test."
        )

    if fill_count is None:
        # Default: consume ~70% of the plane's blocks to leave a healthy GC buffer.
        fill_count = (BLOCK_PER_PLANE // 2) * PAGE_PER_BLOCK   # 32 blocks = 256 pages

    if fill_count > plane_capacity:
        raise ValueError(
            f"fill_count ({fill_count}) exceeds available LPAs ({plane_capacity})"
        )
    if overwrite_count > fill_count:
        raise ValueError(
            f"overwrite_count ({overwrite_count}) must be ≤ fill_count ({fill_count})"
        )

    rng = random.Random(seed)
    commands: list[dict[str, Any]] = []
    written_lpas: set[int] = set()

    # ---- Phase 1: Fill ----
    fill_cmds = build_fill_phase(available_lpas, fill_count, start_time=0, time_step=time_step_ns)
    commands.extend(fill_cmds)
    written_lpas.update(available_lpas[:fill_count])

    current_time = len(commands) * time_step_ns

    # ---- Phase 2: Overwrite ----
    overwrite_targets = available_lpas[:overwrite_count]
    overwrite_cmds = build_overwrite_phase(
        available_lpas, overwrite_targets,
        start_time=current_time, time_step=time_step_ns,
    )
    commands.extend(overwrite_cmds)
    current_time = len(commands) * time_step_ns

    # ---- Phase 3: Sustained mixed ----
    sustained_cmds, written_lpas = build_sustained_phase(
        available_lpas=available_lpas,
        written_lpas=written_lpas,
        rng=rng,
        start_time=current_time,
        time_step=time_step_ns,
        num_cycles=sustained_cycles,
        writes_per_cycle=writes_per_cycle,
        reads_per_cycle=reads_per_cycle,
        max_unique_lpas=plane_capacity,
    )
    commands.extend(sustained_cmds)
    current_time = len(commands) * time_step_ns

    # ---- Phase 4: Final reads ----
    final_cmds = build_final_read_phase(
        written_lpas=list(written_lpas),
        rng=rng,
        count=final_reads,
        start_time=current_time,
        time_step=time_step_ns,
    )
    commands.extend(final_cmds)

    return commands


def generate_low_invalid_trace(*, time_step_ns: int = 2_000_000) -> list[dict[str, Any]]:
    """Reach the GC watermark with one invalid page in the victim block."""
    lpas = enumerate_lpas_for_plane((0, 0, 0, 0))
    fill_count = MIN_WRITES_FOR_GC - 1
    commands = build_fill_phase(lpas, fill_count, time_step=time_step_ns)
    commands.append(_make_write(lpas[0], len(commands) * time_step_ns))
    commands.extend(
        build_final_read_phase(
            lpas[:fill_count],
            random.Random(11),
            count=8,
            start_time=len(commands) * time_step_ns,
            time_step=time_step_ns,
        )
    )
    return commands


def generate_concurrent_overwrite_trace() -> list[dict[str, Any]]:
    """Submit fill and overwrite phases concurrently to stress cache/GC races."""
    return [
        command
        for command in generate_trace(seed=17, time_step_ns=0)
        if command["type"] == "write"
    ]


def generate_post_flush_sustained_trace(*, time_step_ns: int = 2_000_000) -> list[dict[str, Any]]:
    """Pause after the fill phase, then sustain overwrites after cache writeback."""
    commands = generate_trace(seed=23, time_step_ns=time_step_ns)
    fill_count = (BLOCK_PER_PLANE // 2) * PAGE_PER_BLOCK
    pause_ns = 50_000_000
    for command in commands[fill_count:]:
        command["time"] += pause_ns
    return commands


def generate_gc_reoverwrite_trace(*, time_step_ns: int = 2_000_000) -> list[dict[str, Any]]:
    """Overwrite a live victim LPA again while its GC relocation is in flight."""
    lpas = enumerate_lpas_for_plane((0, 0, 0, 0))
    fill_count = MIN_WRITES_FOR_GC - 1
    commands = build_fill_phase(lpas, fill_count, time_step=time_step_ns)
    trigger_time = len(commands) * time_step_ns
    commands.append(_make_write(lpas[0], trigger_time))
    commands.append(_make_write(lpas[1], trigger_time + 1_000_000))
    commands.append(_make_read(lpas[0], trigger_time + 30_000_000))
    commands.append(_make_read(lpas[1], trigger_time + 32_000_000))
    return commands


def generate_wide_trace(*, time_step_ns: int = 20_000_000) -> list[dict[str, Any]]:
    """Keep one plane under GC pressure while expanding writes to a second plane."""
    primary = enumerate_lpas_for_plane((0, 0, 0, 0))
    secondary = enumerate_lpas_for_plane((0, 0, 0, 1))
    commands = build_fill_phase(primary, MIN_WRITES_FOR_GC - 1, time_step=time_step_ns)
    commands.append(_make_write(primary[0], len(commands) * time_step_ns))
    for lpa in secondary[:32]:
        commands.append(_make_write(lpa, len(commands) * time_step_ns))
    commands.extend(
        build_final_read_phase(
            primary[: MIN_WRITES_FOR_GC - 1] + secondary[:32],
            random.Random(29),
            count=16,
            start_time=len(commands) * time_step_ns,
            time_step=time_step_ns,
        )
    )
    return commands


def generate_scenario_trace(
    scenario: str,
    *,
    seed: int = 0,
    target_plane: tuple[int, int, int, int] = (0, 0, 0, 0),
    fill_count: int | None = None,
    overwrite_count: int = 64,
    sustained_cycles: int = 30,
    writes_per_cycle: int = 8,
    reads_per_cycle: int = 2,
    final_reads: int = 16,
    time_step_ns: int = SAFE_TIME_STEP_NS,
) -> list[dict[str, Any]]:
    if scenario == "standard":
        return generate_trace(
            seed=seed,
            target_plane=target_plane,
            fill_count=fill_count,
            overwrite_count=overwrite_count,
            sustained_cycles=sustained_cycles,
            writes_per_cycle=writes_per_cycle,
            reads_per_cycle=reads_per_cycle,
            final_reads=final_reads,
            time_step_ns=time_step_ns,
        )
    if scenario == "low-invalid":
        return generate_low_invalid_trace(time_step_ns=time_step_ns)
    if scenario == "concurrent-overwrite":
        return generate_concurrent_overwrite_trace()
    if scenario == "post-flush-sustained":
        return generate_post_flush_sustained_trace(time_step_ns=time_step_ns)
    if scenario == "gc-reoverwrite":
        return generate_gc_reoverwrite_trace(time_step_ns=time_step_ns)
    if scenario == "wide":
        return generate_wide_trace(time_step_ns=time_step_ns)
    raise ValueError(f"Unknown GC pressure scenario: {scenario}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a write-intensive GC pressure trace for Flash-Sim.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--scenario",
        choices=(
            "standard",
            "low-invalid",
            "concurrent-overwrite",
            "post-flush-sustained",
            "gc-reoverwrite",
            "wide",
        ),
        default="standard",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--target-channel", type=int, default=0)
    parser.add_argument("--target-chip", type=int, default=0,
                        help=f"Non-static chip index [0, {_NON_STATIC_CHIPS - 1}]")
    parser.add_argument("--target-die", type=int, default=0)
    parser.add_argument("--target-plane", type=int, default=0)
    parser.add_argument("--fill-count", type=int, default=None)
    parser.add_argument("--overwrite-count", type=int, default=64)
    parser.add_argument("--sustained-cycles", type=int, default=30)
    parser.add_argument("--writes-per-cycle", type=int, default=8)
    parser.add_argument("--reads-per-cycle", type=int, default=2)
    parser.add_argument("--final-reads", type=int, default=16)
    parser.add_argument("--time-step", type=int, default=SAFE_TIME_STEP_NS,
                        help=f"Nanoseconds between consecutive requests (default: {SAFE_TIME_STEP_NS} ns = 500 µs)")
    parser.add_argument("--summary", action="store_true", default=True,
                        help="Print summary after generation (default).")
    parser.add_argument("--no-summary", action="store_false", dest="summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    target = (args.target_channel, args.target_chip, args.target_die, args.target_plane)

    if args.target_chip >= _NON_STATIC_CHIPS:
        print(
            f"Error: target chip {args.target_chip} is static. "
            f"Use a non-static chip [0, {_NON_STATIC_CHIPS - 1}].",
            file=sys.stderr,
        )
        return 1

    trace = generate_scenario_trace(
        args.scenario,
        seed=args.seed,
        target_plane=target,
        fill_count=args.fill_count,
        overwrite_count=args.overwrite_count,
        sustained_cycles=args.sustained_cycles,
        writes_per_cycle=args.writes_per_cycle,
        reads_per_cycle=args.reads_per_cycle,
        final_reads=args.final_reads,
        time_step_ns=args.time_step,
    )

    output_path = _normalize_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")

    if args.summary:
        type_counts = dict(Counter(cmd["type"] for cmd in trace))
        total = len(trace)
        print(f"[generate_gc_pressure_trace] Wrote {total} requests to {output_path}")
        print(f"[generate_gc_pressure_trace] Target plane: {target}")
        print(f"[generate_gc_pressure_trace] Type counts: {type_counts}")
        read_pct = type_counts.get("read", 0) / total * 100 if total else 0
        print(f"[generate_gc_pressure_trace] Read ratio: {read_pct:.1f}%")
        print(
            f"[generate_gc_pressure_trace] Fill: {args.fill_count or (BLOCK_PER_PLANE // 2) * PAGE_PER_BLOCK}  "
            f"Overwrite: {args.overwrite_count}  "
            f"Sustained: {args.sustained_cycles}×({args.writes_per_cycle}w+{args.reads_per_cycle}r)  "
            f"Final reads: {args.final_reads}  "
            f"Time step: {args.time_step}ns"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
