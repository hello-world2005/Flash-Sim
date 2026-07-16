"""Generate a topology-aware engine trace for repository regression tests."""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


if __package__ in (None, ""):
    _HERE = Path(__file__).resolve().parent
    _REPO_ROOT = _HERE.parent
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
else:
    _REPO_ROOT = Path(__file__).resolve().parents[1]


from flash_sim.FTL import Address_Mapping_Unit, Block_Manager
from flash_sim.PHY import PHY
from flash_sim.common import (
    BLOCK_PER_PLANE,
    CHANNEL_NO,
    CHIP_PER_CHANNEL,
    DIE_PER_CHIP,
    GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
    PAGE_PER_BLOCK,
    PLANE_PER_DIE,
    SECTOR_PER_PAGE,
    SL_PER_BLOCK,
    SSL_PER_SL,
    STATIC_BASE_LHA,
    STATIC_CHIP_PER_CHANNEL,
)


DEFAULT_OUTPUT_PATH = _REPO_ROOT / "test_case" / "test_trace.json"
DEFAULT_PRE_DATA_PATH = _REPO_ROOT / "pre_data" / "precondition_data.json"
DEFAULT_REQUEST_BUDGET = 16
MIN_REQUEST_BUDGET = 8
TIME_STEP_NS = 10


@dataclass(frozen=True)
class PlaneKey:
    channel: int
    chip: int
    die: int
    plane: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.channel, self.chip, self.die, self.plane)


@dataclass(frozen=True)
class PlaneSnapshot:
    key: PlaneKey
    free_block_count: int
    valid_page_count: int
    invalid_page_count: int
    write_frontier_block: int
    write_frontier_page: int
    write_frontier_remaining: int
    frontier_in_free_pool: bool
    valid_lpas: tuple[int, ...]
    candidate_lpas: tuple[int, ...]
    unused_lpas: tuple[int, ...]


@dataclass(frozen=True)
class RuntimeContext:
    pre_data_path: Path
    precondition_records: tuple[dict[str, Any], ...]
    plane_snapshots: tuple[PlaneSnapshot, ...]
    random_access_data_pages: int
    total_random_access_pages: int
    mapping_page_count: int
    static_start_lha: int
    static_end_lha: int

    @property
    def plane_map(self) -> dict[PlaneKey, PlaneSnapshot]:
        return {plane.key: plane for plane in self.plane_snapshots}


@dataclass(frozen=True)
class PendingCommand:
    request_id: str
    command_type: str
    start_lha: int
    size: int
    depends_on: tuple[str, ...] = ()
    plane_key: PlaneKey | None = None
    lpa: int | None = None
    role: str = "generic"
    selected_wl: int | None = None

    def to_trace_entry(self, time_ns: int) -> dict[str, Any]:
        entry = {
            "type": self.command_type,
            "time": time_ns,
            "start_lha": self.start_lha,
            "size": self.size,
        }
        if self.command_type == "compute":
            entry["selected_wl"] = self.selected_wl
        return entry


@dataclass(frozen=True)
class TraceSummary:
    seed: int
    request_budget: int
    target_plane: PlaneKey
    target_plane_free_blocks_before: int
    target_plane_invalid_pages_before: int
    target_plane_write_frontier_remaining: int
    required_gc_pressure_writes: int
    gc_pressure_write_lpas: tuple[int, ...]
    overwrite_lpas: tuple[int, ...]
    precondition_read_lpas: tuple[int, ...]
    readback_write_ids: tuple[str, ...]
    type_counts: dict[str, int]


@dataclass(frozen=True)
class GeneratedTrace:
    commands: tuple[dict[str, Any], ...]
    ordered_requests: tuple[PendingCommand, ...]
    summary: TraceSummary

    def write_json(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(list(self.commands), indent=2),
            encoding="utf-8",
        )
        return output_path


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _make_runtime_fixture(pre_data_path: Path) -> tuple[Block_Manager, PHY, Address_Mapping_Unit]:
    block_manager = Block_Manager()
    phy = PHY()
    amu = Address_Mapping_Unit()
    amu.block_manager = block_manager
    block_manager.preconditioning(data_path=str(pre_data_path), phy=phy, amu=amu)
    return block_manager, phy, amu


def _static_end_lha() -> int:
    return STATIC_BASE_LHA + (
        CHANNEL_NO
        * STATIC_CHIP_PER_CHANNEL
        * DIE_PER_CHIP
        * PLANE_PER_DIE
        * BLOCK_PER_PLANE
        * SL_PER_BLOCK
        * SSL_PER_SL
    )


def _load_precondition_records(pre_data_path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(pre_data_path.read_text(encoding="utf-8")))


def _plane_key_for_lpa(amu: Address_Mapping_Unit, lpa: int) -> PlaneKey:
    address = amu.get_plane_address_for_lpa(lpa)
    return PlaneKey(
        channel=address.channel,
        chip=address.chip,
        die=address.die,
        plane=address.plane,
    )


def plane_key_for_random_access_lpa(lpa: int) -> PlaneKey:
    pages_per_plane = BLOCK_PER_PLANE * PAGE_PER_BLOCK
    plane_value = lpa // pages_per_plane
    plane = plane_value % PLANE_PER_DIE
    plane_value //= PLANE_PER_DIE
    die = plane_value % DIE_PER_CHIP
    plane_value //= DIE_PER_CHIP
    chip = plane_value % CHIP_PER_CHANNEL
    channel = plane_value // CHIP_PER_CHANNEL
    return PlaneKey(channel=channel, chip=chip, die=die, plane=plane)


def _group_candidate_lpas(amu: Address_Mapping_Unit) -> dict[PlaneKey, list[int]]:
    grouped: dict[PlaneKey, list[int]] = defaultdict(list)
    for lpa in range(amu.random_access_data_pages):
        grouped[_plane_key_for_lpa(amu, lpa)].append(lpa)
    return grouped


def _group_valid_lpas(
    amu: Address_Mapping_Unit,
    records: Iterable[dict[str, Any]],
) -> dict[PlaneKey, list[int]]:
    grouped: dict[PlaneKey, list[int]] = defaultdict(list)
    for record in records:
        lpa = int(record["lpa"])
        if lpa < 0 or lpa >= amu.random_access_data_pages:
            continue
        grouped[_plane_key_for_lpa(amu, lpa)].append(lpa)
    return grouped


def _snapshot_planes(
    block_manager: Block_Manager,
    amu: Address_Mapping_Unit,
    valid_lpas_by_plane: dict[PlaneKey, list[int]],
    candidate_lpas_by_plane: dict[PlaneKey, list[int]],
) -> tuple[PlaneSnapshot, ...]:
    snapshots: list[PlaneSnapshot] = []
    for channel in range(block_manager.channel_no):
        for chip in range(block_manager.chip_no_per_channel - STATIC_CHIP_PER_CHANNEL):
            for die in range(block_manager.die_no_per_chip):
                for plane in range(block_manager.plane_no_per_die):
                    key = PlaneKey(channel=channel, chip=chip, die=die, plane=plane)
                    plane_bke = block_manager.block_keeping_book[channel][chip][die][plane]
                    valid_lpas = tuple(sorted(valid_lpas_by_plane.get(key, [])))
                    candidate_lpas = tuple(sorted(candidate_lpas_by_plane.get(key, [])))
                    valid_set = set(valid_lpas)
                    unused_lpas = tuple(lpa for lpa in candidate_lpas if lpa not in valid_set)
                    write_frontier_block = plane_bke.write_frontier_block
                    frontier_page = plane_bke.block_entries[write_frontier_block].write_frontier
                    snapshots.append(
                        PlaneSnapshot(
                            key=key,
                            free_block_count=len(plane_bke.free_block_pool),
                            valid_page_count=plane_bke.valid_page_count,
                            invalid_page_count=plane_bke.invalid_page_count,
                            write_frontier_block=write_frontier_block,
                            write_frontier_page=frontier_page,
                            write_frontier_remaining=PAGE_PER_BLOCK - frontier_page,
                            frontier_in_free_pool=write_frontier_block in plane_bke.free_block_pool,
                            valid_lpas=valid_lpas,
                            candidate_lpas=candidate_lpas,
                            unused_lpas=unused_lpas,
                        )
                    )
    return tuple(snapshots)


@lru_cache(maxsize=4)
def build_runtime_context(pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH) -> RuntimeContext:
    normalized_pre_data = _normalize_path(pre_data_path)
    records = _load_precondition_records(normalized_pre_data)
    block_manager, _phy, amu = _make_runtime_fixture(normalized_pre_data)
    candidate_lpas_by_plane = _group_candidate_lpas(amu)
    valid_lpas_by_plane = _group_valid_lpas(amu, records)
    plane_snapshots = _snapshot_planes(
        block_manager,
        amu,
        valid_lpas_by_plane,
        candidate_lpas_by_plane,
    )
    return RuntimeContext(
        pre_data_path=normalized_pre_data,
        precondition_records=records,
        plane_snapshots=plane_snapshots,
        random_access_data_pages=amu.random_access_data_pages,
        total_random_access_pages=amu.total_random_access_pages,
        mapping_page_count=amu.mapping_page_count,
        static_start_lha=STATIC_BASE_LHA,
        static_end_lha=_static_end_lha(),
    )


def valid_sector_runs(valid_bitmap: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, bit in enumerate(valid_bitmap):
        if bit and start is None:
            start = index
        elif not bit and start is not None:
            runs.append((start, index - start))
            start = None
    if start is not None:
        runs.append((start, len(valid_bitmap) - start))
    return runs


def estimate_gc_pressure_write_count(
    plane_snapshot: PlaneSnapshot,
    threshold: int = GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD,
) -> int:
    decrements_needed = max(0, plane_snapshot.free_block_count - threshold)
    if decrements_needed == 0:
        return 0

    if plane_snapshot.frontier_in_free_pool:
        first_decrement_writes = 1
    else:
        first_decrement_writes = plane_snapshot.write_frontier_remaining + 1

    return first_decrement_writes + max(0, decrements_needed - 1) * PAGE_PER_BLOCK


def select_target_plane(context: RuntimeContext) -> PlaneSnapshot:
    candidates = [
        plane
        for plane in context.plane_snapshots
        if plane.valid_page_count > 0 or plane.invalid_page_count > 0
    ]
    if not candidates:
        raise ValueError("no preconditioned random-access plane is available")

    return min(
        candidates,
        key=lambda plane: (
            0 if plane.invalid_page_count > 0 else 1,
            plane.free_block_count,
            plane.write_frontier_remaining,
            -plane.valid_page_count,
            plane.key.as_tuple(),
        ),
    )


def _pick_precondition_records(
    context: RuntimeContext,
    rng: random.Random,
    count: int,
    excluded_lpas: set[int],
) -> list[dict[str, Any]]:
    valid_plane_keys = {plane.key for plane in context.plane_snapshots}
    eligible = [
        record
        for record in context.precondition_records
        if int(record["lpa"]) not in excluded_lpas
        and plane_key_for_random_access_lpa(int(record["lpa"])) in valid_plane_keys
        and valid_sector_runs(record["valid_bitmap"])
    ]
    if len(eligible) < count:
        raise ValueError("not enough precondition records to satisfy read sampling")
    return rng.sample(eligible, count)


def build_precondition_read_requests(
    context: RuntimeContext,
    rng: random.Random,
    count: int,
    excluded_lpas: set[int] | None = None,
) -> tuple[list[PendingCommand], tuple[int, ...]]:
    excluded_lpas = set(excluded_lpas or ())
    requests: list[PendingCommand] = []
    selected_lpas: list[int] = []
    for index, record in enumerate(_pick_precondition_records(context, rng, count, excluded_lpas)):
        runs = valid_sector_runs(record["valid_bitmap"])
        sector_offset, run_length = rng.choice(runs)
        read_length = rng.randint(1, run_length)
        lpa = int(record["lpa"])
        requests.append(
            PendingCommand(
                request_id=f"pre-read-{index:03d}",
                command_type="read",
                start_lha=lpa * SECTOR_PER_PAGE + sector_offset,
                size=read_length,
                lpa=lpa,
                role="precondition-read",
            )
        )
        selected_lpas.append(lpa)
    return requests, tuple(selected_lpas)


def build_static_requests(
    context: RuntimeContext,
    rng: random.Random,
) -> list[PendingCommand]:
    span = context.static_end_lha - context.static_start_lha
    search_size = 1
    compute_size = 1
    max_search_start = context.static_end_lha - search_size
    max_compute_start = context.static_end_lha - compute_size
    search_start = rng.randint(context.static_start_lha, max_search_start)
    compute_start = rng.randint(context.static_start_lha, max_compute_start)
    return [
        PendingCommand(
            request_id="static-search-000",
            command_type="search",
            start_lha=search_start,
            size=search_size,
            role="static-search",
        ),
        PendingCommand(
            request_id="static-compute-000",
            command_type="compute",
            start_lha=compute_start,
            size=compute_size,
            role="static-compute",
            selected_wl=0,
        ),
    ]


def _resolve_mix_counts(request_budget: int) -> tuple[int, int]:
    budget = max(MIN_REQUEST_BUDGET, request_budget)
    remaining = max(2, budget - 2)
    pair_count = max(1, remaining // 3)
    precondition_read_count = max(1, remaining - pair_count * 2)
    return precondition_read_count, pair_count


def _pick_overwrite_lpa(
    target_plane: PlaneSnapshot,
    protected_lpas: set[int],
    rng: random.Random,
) -> int:
    candidates = [lpa for lpa in target_plane.valid_lpas if lpa not in protected_lpas]
    if not candidates:
        raise ValueError("target plane lacks a safe valid LPA for overwrite-based invalidation")
    return rng.choice(candidates)


def build_gc_pressure_write_requests(
    target_plane: PlaneSnapshot,
    protected_lpas: set[int],
    required_writes: int,
    rng: random.Random,
) -> tuple[list[PendingCommand], tuple[int, ...], tuple[int, ...]]:
    if required_writes <= 0:
        return [], (), ()

    requests: list[PendingCommand] = []
    selected_lpas: list[int] = []
    overwrite_lpas: list[int] = []

    if target_plane.invalid_page_count == 0:
        overwrite_lpa = _pick_overwrite_lpa(target_plane, protected_lpas, rng)
        requests.append(
            PendingCommand(
                request_id="gc-write-0000",
                command_type="write",
                start_lha=overwrite_lpa * SECTOR_PER_PAGE,
                size=SECTOR_PER_PAGE,
                plane_key=target_plane.key,
                lpa=overwrite_lpa,
                role="gc-pressure-overwrite",
            )
        )
        selected_lpas.append(overwrite_lpa)
        overwrite_lpas.append(overwrite_lpa)

    available_unused_lpas = [lpa for lpa in target_plane.unused_lpas if lpa not in protected_lpas]
    remaining_writes = required_writes - len(requests)
    if remaining_writes > len(available_unused_lpas):
        raise ValueError(
            f"target plane {target_plane.key.as_tuple()} does not have enough unused LPAs "
            f"for {required_writes} GC-pressure writes"
        )

    chosen_unused_lpas = rng.sample(available_unused_lpas, remaining_writes)
    for offset, lpa in enumerate(chosen_unused_lpas, start=len(requests)):
        requests.append(
            PendingCommand(
                request_id=f"gc-write-{offset:04d}",
                command_type="write",
                start_lha=lpa * SECTOR_PER_PAGE,
                size=SECTOR_PER_PAGE,
                plane_key=target_plane.key,
                lpa=lpa,
                role="gc-pressure-write",
            )
        )
        selected_lpas.append(lpa)

    return requests, tuple(selected_lpas), tuple(overwrite_lpas)


def _chunked_readback_indices(
    total_writes: int,
    readback_count: int,
    rng: random.Random,
) -> list[int]:
    if total_writes <= 0:
        return []

    indices: list[int] = []
    used: set[int] = set()
    for index in range(min(total_writes, readback_count)):
        start = index * total_writes // readback_count
        end = max(start + 1, (index + 1) * total_writes // readback_count)
        candidates = [candidate for candidate in range(start, end) if candidate not in used]
        if not candidates:
            candidates = [candidate for candidate in range(total_writes) if candidate not in used]
        chosen = rng.choice(candidates)
        used.add(chosen)
        indices.append(chosen)
    return sorted(indices)


def build_readback_requests(
    write_requests: list[PendingCommand],
    readback_count: int,
    rng: random.Random,
) -> tuple[list[PendingCommand], tuple[str, ...]]:
    readbacks: list[PendingCommand] = []
    bound_write_ids: list[str] = []
    for index in _chunked_readback_indices(len(write_requests), readback_count, rng):
        write_request = write_requests[index]
        readbacks.append(
            PendingCommand(
                request_id=f"readback-{len(readbacks):03d}",
                command_type="read",
                start_lha=write_request.start_lha,
                size=max(1, SECTOR_PER_PAGE // 2),
                depends_on=(write_request.request_id,),
                plane_key=write_request.plane_key,
                lpa=write_request.lpa,
                role="write-readback",
            )
        )
        bound_write_ids.append(write_request.request_id)
    return readbacks, tuple(bound_write_ids)


def schedule_requests(
    requests: Iterable[PendingCommand],
    rng: random.Random,
) -> tuple[PendingCommand, ...]:
    pending = {request.request_id: request for request in requests}
    emitted: list[PendingCommand] = []
    completed_ids: set[str] = set()
    last_type: str | None = None

    while pending:
        available = [
            request
            for request in pending.values()
            if set(request.depends_on).issubset(completed_ids)
        ]
        if not available:
            raise ValueError("request dependencies are cyclic or incomplete")

        alternatives = [request for request in available if request.command_type != last_type]
        pool = alternatives or available
        chosen = rng.choice(pool)
        emitted.append(chosen)
        completed_ids.add(chosen.request_id)
        last_type = chosen.command_type
        pending.pop(chosen.request_id)

    return tuple(emitted)


def _type_counts(commands: Iterable[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(command["type"] for command in commands))


def generate_trace(
    *,
    seed: int = 0,
    pre_data_path: str | Path = DEFAULT_PRE_DATA_PATH,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
) -> GeneratedTrace:
    context = build_runtime_context(pre_data_path)
    target_plane = select_target_plane(context)
    required_writes = estimate_gc_pressure_write_count(target_plane)
    rng = random.Random(seed)

    precondition_read_count, readback_count = _resolve_mix_counts(request_budget)
    protected_lpas: set[int] = set()

    gc_write_requests, gc_write_lpas, overwrite_lpas = build_gc_pressure_write_requests(
        target_plane,
        protected_lpas,
        required_writes,
        rng,
    )
    protected_lpas.update(gc_write_lpas)

    precondition_reads, precondition_read_lpas = build_precondition_read_requests(
        context,
        rng,
        precondition_read_count,
        protected_lpas,
    )
    protected_lpas.update(precondition_read_lpas)

    static_requests = build_static_requests(context, rng)
    readback_requests, readback_write_ids = build_readback_requests(
        gc_write_requests,
        readback_count,
        rng,
    )

    ordered_requests = schedule_requests(
        [*precondition_reads, *gc_write_requests, *readback_requests, *static_requests],
        rng,
    )
    commands = tuple(
        request.to_trace_entry(index * TIME_STEP_NS)
        for index, request in enumerate(ordered_requests)
    )

    return GeneratedTrace(
        commands=commands,
        ordered_requests=ordered_requests,
        summary=TraceSummary(
            seed=seed,
            request_budget=max(MIN_REQUEST_BUDGET, request_budget),
            target_plane=target_plane.key,
            target_plane_free_blocks_before=target_plane.free_block_count,
            target_plane_invalid_pages_before=target_plane.invalid_page_count,
            target_plane_write_frontier_remaining=target_plane.write_frontier_remaining,
            required_gc_pressure_writes=required_writes,
            gc_pressure_write_lpas=gc_write_lpas,
            overwrite_lpas=overwrite_lpas,
            precondition_read_lpas=precondition_read_lpas,
            readback_write_ids=readback_write_ids,
            type_counts=_type_counts(commands),
        ),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0, help="Random seed for reproducible traces.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Output path for the generated engine trace JSON.",
    )
    parser.add_argument(
        "--pre-data",
        type=Path,
        default=DEFAULT_PRE_DATA_PATH,
        help="Path to precondition_data.json.",
    )
    parser.add_argument(
        "--request-budget",
        type=int,
        default=DEFAULT_REQUEST_BUDGET,
        help="Target number of non-GC requests mixed into the generated trace.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    generated = generate_trace(
        seed=args.seed,
        pre_data_path=args.pre_data,
        request_budget=args.request_budget,
    )
    output_path = generated.write_json(_normalize_path(args.output))

    summary = generated.summary
    print(f"[generate_test_trace] Wrote {len(generated.commands)} requests to {output_path}")
    print(
        "[generate_test_trace] Target plane: "
        f"{summary.target_plane.as_tuple()} "
        f"free_blocks_before={summary.target_plane_free_blocks_before} "
        f"invalid_pages_before={summary.target_plane_invalid_pages_before} "
        f"frontier_remaining={summary.target_plane_write_frontier_remaining}"
    )
    print(
        "[generate_test_trace] GC pressure writes: "
        f"{summary.required_gc_pressure_writes} "
        f"(overwrites={len(summary.overwrite_lpas)})"
    )
    print(f"[generate_test_trace] Type counts: {summary.type_counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
