#!/usr/bin/env python3
"""Minimal Flash-Sim vs MQSim validation harness.

The default case compares a generated full-page write trace on both simulators.
It is a conservative starting point: the script validates shared request
semantics and records latency/accounting differences without pretending that the
two simulators expose identical host-completion timing today.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


FLASH_SIM_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = FLASH_SIM_ROOT.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    return Path(os.path.expandvars(raw)).expanduser().resolve()


MQSIM_ROOT = _env_path("MQSIM_ROOT", WORKSPACE_ROOT / "MQSim")
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "out"

FLASHSIM_SECTOR_SIZE_BYTES = 64
FLASHSIM_SECTORS_PER_PAGE = 64
FLASHSIM_DATA_CACHE_LINES = 64
MQSIM_SECTOR_SIZE_BYTES = 512
MQSIM_OVERPROVISIONING_RATIO = 0.07
MQSIM_LATENCY_FIELDS_US = {
    "Device_Response_Time",
    "Min_Device_Response_Time",
    "Max_Device_Response_Time",
    "End_to_End_Request_Delay",
    "Min_End_to_End_Request_Delay",
    "Max_End_to_End_Request_Delay",
}
FLASHSIM_TRANSACTION_LATENCY_STAGES = (
    "tsu_queue_wait",
    "phy_cmd_addr",
    "phy_data_in",
    "phy_array_exec",
    "phy_data_out",
)
FLASHSIM_READ_SERVICE_STAGES = ("phy_cmd_addr", "phy_array_exec", "phy_data_out")
FLASHSIM_WRITE_SERVICE_STAGES = ("phy_cmd_addr", "phy_data_in", "phy_array_exec")
FLASHSIM_MAPPING_ENTRIES_PER_PAGE = 256


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    page_capacity: int
    page_metadata_capacity: int
    page_no_per_block: int
    block_no_per_plane: int
    plane_no_per_die: int
    die_no_per_chip: int
    flash_channel_count: int
    chip_no_per_channel: int
    flash_channel_width: int
    channel_transfer_rate: int
    flash_technology: str
    page_read_latency_lsb: int
    page_program_latency_lsb: int
    block_erase_latency: int
    data_cache_capacity: int
    cmt_capacity: int
    ideal_mapping_table: bool
    enabled_preconditioning: bool
    queue_fetch_size: int

    @property
    def mqsim_sectors_per_page(self) -> int:
        if self.page_capacity % MQSIM_SECTOR_SIZE_BYTES != 0:
            raise ValueError(f"{self.name}: page size must be a multiple of 512 B")
        return self.page_capacity // MQSIM_SECTOR_SIZE_BYTES


PROFILES: dict[str, Profile] = {
    "flashsim-event": Profile(
        name="flashsim-event",
        description="MQSim adapted to Flash-Sim event-runtime timing with modern block geometry.",
        page_capacity=4096,
        page_metadata_capacity=448,
        page_no_per_block=512,
        block_no_per_plane=2048,
        plane_no_per_die=4,
        die_no_per_chip=4,
        flash_channel_count=8,
        chip_no_per_channel=3,
        flash_channel_width=8,
        channel_transfer_rate=333,
        flash_technology="SLC",
        page_read_latency_lsb=5000,
        page_program_latency_lsb=250000,
        block_erase_latency=10000000,
        data_cache_capacity=4096 * 64,
        cmt_capacity=2097152,
        ideal_mapping_table=True,
        enabled_preconditioning=False,
        queue_fetch_size=512,
    ),
    "flashsim-event-small": Profile(
        name="flashsim-event-small",
        description="Original small Flash-Sim event-runtime geometry/timing used for GC-pressure validation.",
        page_capacity=4096,
        page_metadata_capacity=448,
        page_no_per_block=8,
        block_no_per_plane=64,
        plane_no_per_die=4,
        die_no_per_chip=4,
        flash_channel_count=8,
        chip_no_per_channel=3,
        flash_channel_width=8,
        channel_transfer_rate=333,
        flash_technology="SLC",
        page_read_latency_lsb=5000,
        page_program_latency_lsb=250000,
        block_erase_latency=10000000,
        data_cache_capacity=4096 * 64,
        cmt_capacity=2097152,
        ideal_mapping_table=True,
        enabled_preconditioning=False,
        queue_fetch_size=512,
    ),
    "fast18-paper": Profile(
        name="fast18-paper",
        description="MQSim FAST'18 paper SSD configuration.",
        page_capacity=8192,
        page_metadata_capacity=448,
        page_no_per_block=256,
        block_no_per_plane=2048,
        plane_no_per_die=2,
        die_no_per_chip=2,
        flash_channel_count=8,
        chip_no_per_channel=4,
        flash_channel_width=1,
        channel_transfer_rate=333,
        flash_technology="MLC",
        page_read_latency_lsb=75000,
        page_program_latency_lsb=750000,
        block_erase_latency=3800000,
        data_cache_capacity=268435456,
        cmt_capacity=4194304,
        ideal_mapping_table=False,
        enabled_preconditioning=False,
        queue_fetch_size=512,
    ),
}

PROFILES["flashsim-event-finite-cmt"] = replace(
    PROFILES["flashsim-event"],
    name="flashsim-event-finite-cmt",
    description=(
        "MQSim adapted to Flash-Sim event-runtime geometry/timing, with "
        "ideal mapping disabled and finite CMT capacity for mapping-path validation."
    ),
    cmt_capacity=4096,
    ideal_mapping_table=False,
)


def flashsim_event_runtime_env(profile: Profile) -> dict[str, str]:
    """Environment overrides consumed by flash_sim.config at import time."""
    if profile.page_no_per_block == 512:
        layers_per_block = 128
        sl_per_block = 1
        ssl_per_sl = 4
    elif profile.page_no_per_block == 8:
        layers_per_block = 1
        sl_per_block = 2
        ssl_per_sl = 4
    else:
        layers_per_block = profile.page_no_per_block
        sl_per_block = 1
        ssl_per_sl = 1
    return {
        "FLASHSIM_EVENT_RUNTIME_DIES": str(profile.die_no_per_chip),
        "FLASHSIM_EVENT_RUNTIME_PLANES_PER_DIE": str(profile.plane_no_per_die),
        "FLASHSIM_EVENT_RUNTIME_BLOCKS_PER_PLANE": str(profile.block_no_per_plane),
        "FLASHSIM_EVENT_RUNTIME_LAYERS_PER_BLOCK": str(layers_per_block),
        "FLASHSIM_EVENT_RUNTIME_SL_PER_BLOCK": str(sl_per_block),
        "FLASHSIM_EVENT_RUNTIME_SSL_PER_SL": str(ssl_per_sl),
    }


@dataclass(frozen=True)
class GeneratedCase:
    name: str
    operations: list[dict[str, Any]]
    flash_precondition_records: list[dict[str, Any]] | None = None
    mqsim_warmup_operations: list[dict[str, Any]] | None = None
    mqsim_main_time_shift_ns: int = 0
    mqsim_enabled_preconditioning: bool = False
    mqsim_initial_occupancy_percentage: int = 0
    mqsim_cache_mode: str = "WRITE_CACHE"
    mqsim_gc_exec_threshold: str | None = None
    mqsim_gc_block_selection_policy: str | None = None
    mqsim_static_wl_threshold: int | None = None
    expect_media_reads: bool = False
    expect_user_programs_equal_writes: bool = True
    expect_gc: bool = False
    expect_static_wl: bool = False
    expect_no_static_wl: bool = False
    expected_gc_count: int | None = None
    expected_static_wl_count: int | None = None
    expected_gc_relocated_pages: int | None = None
    expected_erase_count: int | None = None
    mqsim_maintenance_diagnostic: bool = False
    latency_alignment: bool = False
    direct_latency_compare: bool = False
    flashsim_cache_bypass: bool = False
    flashsim_plane_allocation: str | None = None
    flashsim_runtime_overrides: dict[str, Any] | None = None
    trace_kind: str = "generated"
    external_trace_stats: dict[str, Any] | None = None


def bool_xml(value: bool) -> str:
    return "true" if value else "false"


def csv_ids(count: int) -> str:
    return ",".join(str(i) for i in range(count))


def indent_xml(root: ET.Element) -> None:
    try:
        ET.indent(root, space="\t")
    except AttributeError:
        pass


def write_xml(path: Path, root: ET.Element) -> None:
    indent_xml(root)
    tree = ET.ElementTree(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def add_text(parent: ET.Element, tag: str, value: Any) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def build_ssd_config_xml(
    profile: Profile,
    *,
    enabled_preconditioning: bool | None = None,
    case: GeneratedCase | None = None,
) -> ET.Element:
    root = ET.Element("Execution_Parameter_Set")

    host = ET.SubElement(root, "Host_Parameter_Set")
    add_text(host, "PCIe_Lane_Bandwidth", "1.00000")
    add_text(host, "PCIe_Lane_Count", 4)
    add_text(host, "SATA_Processing_Delay", 400000)
    add_text(host, "Enable_ResponseTime_Logging", "false")
    add_text(host, "ResponseTime_Logging_Period_Length", 1000000)

    device = ET.SubElement(root, "Device_Parameter_Set")
    add_text(device, "Seed", 321)
    preconditioning_enabled = (
        profile.enabled_preconditioning
        if enabled_preconditioning is None
        else enabled_preconditioning
    )
    add_text(device, "Enabled_Preconditioning", bool_xml(preconditioning_enabled))
    add_text(device, "Memory_Type", "FLASH")
    add_text(device, "HostInterface_Type", "NVME")
    add_text(device, "IO_Queue_Depth", 65535)
    add_text(device, "Queue_Fetch_Size", profile.queue_fetch_size)
    add_text(device, "Caching_Mechanism", "ADVANCED")
    add_text(device, "Data_Cache_Sharing_Mode", "SHARED")
    add_text(device, "Data_Cache_Capacity", profile.data_cache_capacity)
    add_text(device, "Data_Cache_DRAM_Row_Size", max(8192, profile.page_capacity))
    add_text(device, "Data_Cache_DRAM_Data_Rate", 100)
    add_text(device, "Data_Cache_DRAM_Data_Busrt_Size", 1)
    add_text(device, "Data_Cache_DRAM_tRCD", 13)
    add_text(device, "Data_Cache_DRAM_tCL", 13)
    add_text(device, "Data_Cache_DRAM_tRP", 13)
    add_text(device, "Address_Mapping", "PAGE_LEVEL")
    add_text(device, "Ideal_Mapping_Table", bool_xml(profile.ideal_mapping_table))
    add_text(device, "CMT_Capacity", profile.cmt_capacity)
    add_text(device, "CMT_Sharing_Mode", "SHARED")
    add_text(device, "Plane_Allocation_Scheme", "CWDP")
    add_text(device, "Transaction_Scheduling_Policy", "PRIORITY_OUT_OF_ORDER")
    add_text(device, "Overprovisioning_Ratio", "0.07")
    add_text(device, "GC_Exec_Threshold", case.mqsim_gc_exec_threshold if case and case.mqsim_gc_exec_threshold is not None else "0.05000")
    add_text(device, "GC_Block_Selection_Policy", case.mqsim_gc_block_selection_policy if case and case.mqsim_gc_block_selection_policy is not None else "RGA")
    add_text(device, "Use_Copyback_for_GC", "false")
    add_text(device, "Preemptible_GC_Enabled", "false")
    add_text(device, "GC_Hard_Threshold", "0.005000")
    add_text(device, "Dynamic_Wearleveling_Enabled", "true")
    add_text(device, "Static_Wearleveling_Enabled", "true")
    add_text(device, "Static_Wearleveling_Threshold", case.mqsim_static_wl_threshold if case and case.mqsim_static_wl_threshold is not None else 100)
    add_text(device, "Preferred_suspend_erase_time_for_read", 700000)
    add_text(device, "Preferred_suspend_erase_time_for_write", 700000)
    add_text(device, "Preferred_suspend_write_time_for_read", 100000)
    add_text(device, "Flash_Channel_Count", profile.flash_channel_count)
    add_text(device, "Flash_Channel_Width", profile.flash_channel_width)
    add_text(device, "Channel_Transfer_Rate", profile.channel_transfer_rate)
    add_text(device, "Chip_No_Per_Channel", profile.chip_no_per_channel)
    add_text(device, "Flash_Comm_Protocol", "NVDDR2")

    flash = ET.SubElement(device, "Flash_Parameter_Set")
    add_text(flash, "Flash_Technology", profile.flash_technology)
    add_text(flash, "CMD_Suspension_Support", "ERASE")
    add_text(flash, "Page_Read_Latency_LSB", profile.page_read_latency_lsb)
    add_text(flash, "Page_Read_Latency_CSB", profile.page_read_latency_lsb)
    add_text(flash, "Page_Read_Latency_MSB", profile.page_read_latency_lsb)
    add_text(flash, "Page_Program_Latency_LSB", profile.page_program_latency_lsb)
    add_text(flash, "Page_Program_Latency_CSB", profile.page_program_latency_lsb)
    add_text(flash, "Page_Program_Latency_MSB", profile.page_program_latency_lsb)
    add_text(flash, "Block_Erase_Latency", profile.block_erase_latency)
    add_text(flash, "Block_PE_Cycles_Limit", 10000)
    add_text(flash, "Suspend_Erase_Time", 700000)
    add_text(flash, "Suspend_Program_Time", 100000)
    add_text(flash, "Die_No_Per_Chip", profile.die_no_per_chip)
    add_text(flash, "Plane_No_Per_Die", profile.plane_no_per_die)
    add_text(flash, "Block_No_Per_Plane", profile.block_no_per_plane)
    add_text(flash, "Page_No_Per_Block", profile.page_no_per_block)
    add_text(flash, "Page_Capacity", profile.page_capacity)
    add_text(flash, "Page_Metadat_Capacity", profile.page_metadata_capacity)
    return root


def build_workload_xml(profile: Profile, mqsim_trace_path: Path, case: GeneratedCase) -> ET.Element:
    root = ET.Element("MQSim_IO_Scenarios")
    scenario = ET.SubElement(root, "IO_Scenario")
    flow = ET.SubElement(scenario, "IO_Flow_Parameter_Set_Trace_Based")
    add_text(flow, "Priority_Class", "HIGH")
    add_text(flow, "Device_Level_Data_Caching_Mode", case.mqsim_cache_mode)
    add_text(flow, "Channel_IDs", csv_ids(profile.flash_channel_count))
    add_text(flow, "Chip_IDs", csv_ids(profile.chip_no_per_channel))
    add_text(flow, "Die_IDs", csv_ids(profile.die_no_per_chip))
    add_text(flow, "Plane_IDs", csv_ids(profile.plane_no_per_die))
    add_text(flow, "Initial_Occupancy_Percentage", case.mqsim_initial_occupancy_percentage)
    rel_trace_path = os.path.relpath(mqsim_trace_path, MQSIM_ROOT)
    add_text(flow, "File_Path", rel_trace_path)
    add_text(flow, "Percentage_To_Be_Executed", 100)
    add_text(flow, "Relay_Count", 1)
    add_text(flow, "Time_Unit", "NANOSECOND")
    return root


def make_full_page_op(profile: Profile, op_type: str, time_ns: int, page_id: int, phase: str | None = None) -> dict[str, Any]:
    operation = {
        "type": op_type,
        "time": time_ns,
        "page_id": page_id,
        "flashsim_start_lha": page_id * FLASHSIM_SECTORS_PER_PAGE,
        "flashsim_size": FLASHSIM_SECTORS_PER_PAGE,
        "mqsim_start_sector": page_id * profile.mqsim_sectors_per_page,
        "mqsim_size_sectors": profile.mqsim_sectors_per_page,
    }
    if phase is not None:
        operation["phase"] = phase
    return operation


def cwdp_page_id(
    profile: Profile,
    *,
    channel: int = 0,
    chip: int = 0,
    die: int = 0,
    plane: int = 0,
    cycle: int = 0,
) -> int:
    """Return the LPA/page id for MQSim's CWDP allocation order.

    MQSim CWDP advances channel first, then chip, die, and plane.  The profile's
    chip count intentionally excludes Flash-Sim's static chip for the current
    traditional-flash comparison profile.
    """

    pages_per_cwdp_cycle = (
        profile.flash_channel_count
        * profile.chip_no_per_channel
        * profile.die_no_per_chip
        * profile.plane_no_per_die
    )
    if not 0 <= channel < profile.flash_channel_count:
        raise ValueError(f"channel {channel} out of range")
    if not 0 <= chip < profile.chip_no_per_channel:
        raise ValueError(f"chip {chip} out of range")
    if not 0 <= die < profile.die_no_per_chip:
        raise ValueError(f"die {die} out of range")
    if not 0 <= plane < profile.plane_no_per_die:
        raise ValueError(f"plane {plane} out of range")
    return (
        cycle * pages_per_cwdp_cycle
        + plane * profile.die_no_per_chip * profile.chip_no_per_channel * profile.flash_channel_count
        + die * profile.chip_no_per_channel * profile.flash_channel_count
        + chip * profile.flash_channel_count
        + channel
    )


def generate_write_stream(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    operations = []
    for index in range(request_count):
        operations.append(make_full_page_op(profile, "write", index * gap_ns, index))
    return GeneratedCase(name="write_stream", operations=operations)


def generate_flush_then_read(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    if request_count > FLASHSIM_DATA_CACHE_LINES:
        raise ValueError(
            f"flush_then_read currently supports at most {FLASHSIM_DATA_CACHE_LINES} read requests"
        )
    setup_write_count = FLASHSIM_DATA_CACHE_LINES + 1
    read_start_time = setup_write_count * gap_ns + max(100_000_000, profile.page_program_latency_lsb * 100)
    operations = []
    for index in range(setup_write_count):
        operations.append(make_full_page_op(profile, "write", index * gap_ns, index, "setup"))
    for index in range(request_count):
        operations.append(make_full_page_op(profile, "read", read_start_time + index * gap_ns, index, "measured"))
    return GeneratedCase(
        name="flush_then_read",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        expect_media_reads=True,
        expect_user_programs_equal_writes=True,
        latency_alignment=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
    )


def generate_rich_aligned(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    measured_count = max(request_count, 256)
    setup_write_count = FLASHSIM_DATA_CACHE_LINES + 32
    measured_start_time = setup_write_count * gap_ns + max(
        100_000_000,
        profile.page_program_latency_lsb * 200,
    )
    operations = []

    for index in range(setup_write_count):
        operations.append(make_full_page_op(profile, "write", index * gap_ns, index, "setup"))

    cold_write_base = 10_000
    for index in range(measured_count):
        pattern = index % 8
        if pattern in (0, 2, 3, 5, 6):
            op_type = "read"
            page_id = (index * 7) % (FLASHSIM_DATA_CACHE_LINES // 2)
        else:
            op_type = "write"
            page_id = cold_write_base + index
        operations.append(make_full_page_op(profile, op_type, measured_start_time + index * gap_ns, page_id, "measured"))

    return GeneratedCase(
        name="rich_aligned",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        expect_media_reads=True,
        expect_user_programs_equal_writes=True,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
    )


def generate_parallel_cwdp(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    """Full-page CWDP resource-boundary trace.

    The case writes and then reads pages selected from known CWDP coordinates:
    channel fanout, chip fanout, die boundary, and plane boundary.  It is meant
    to expose allocation mismatches before GC/WL traces add more moving parts.
    """

    measured_count = max(request_count, 32)
    base_gap = max(gap_ns, 1000)
    settle_gap = max(100_000_000, profile.page_program_latency_lsb * 200)
    operations: list[dict[str, Any]] = []

    channel_pages = [
        cwdp_page_id(profile, channel=channel, chip=0, die=0, plane=0)
        for channel in range(profile.flash_channel_count)
    ]
    chip_pages = [
        cwdp_page_id(profile, channel=0, chip=chip, die=0, plane=0)
        for chip in range(profile.chip_no_per_channel)
    ]
    die_pages = [
        cwdp_page_id(profile, channel=0, chip=0, die=die, plane=0)
        for die in range(profile.die_no_per_chip)
    ]
    plane_pages = [
        cwdp_page_id(profile, channel=0, chip=0, die=0, plane=plane)
        for plane in range(profile.plane_no_per_die)
    ]
    all_pages = sorted(set(channel_pages + chip_pages + die_pages + plane_pages))

    for index, page_id in enumerate(all_pages):
        operations.append(make_full_page_op(profile, "write", index * base_gap, page_id, "setup"))

    read_start_time = len(all_pages) * base_gap + settle_gap
    for index in range(measured_count):
        page_id = all_pages[index % len(all_pages)]
        operations.append(make_full_page_op(profile, "read", read_start_time + index * base_gap, page_id, "measured"))

    write_start_time = read_start_time + measured_count * base_gap + settle_gap
    for index, page_id in enumerate(all_pages):
        cycle_page = page_id + (
            profile.flash_channel_count
            * profile.chip_no_per_channel
            * profile.die_no_per_chip
            * profile.plane_no_per_die
        )
        operations.append(make_full_page_op(profile, "write", write_start_time + index * base_gap, cycle_page, "measured"))

    return GeneratedCase(
        name="parallel_cwdp",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        expect_media_reads=False,
        expect_user_programs_equal_writes=False,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
    )


def generate_overwrite_mapping(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    """Full-page overwrite trace that stays inside the first CWDP die/plane.

    Pages 0..23 are common to both Flash-Sim's current CWDP code and MQSim's
    data-chip-only CWDP profile, so this case isolates overwrite/mapping from
    the static-chip boundary issue.
    """

    measured_count = max(request_count, 48)
    base_gap = max(gap_ns, 1000)
    settle_gap = max(100_000_000, profile.page_program_latency_lsb * 200)
    seed_pages = [
        cwdp_page_id(profile, channel=channel, chip=chip, die=0, plane=0)
        for chip in range(profile.chip_no_per_channel)
        for channel in range(profile.flash_channel_count)
    ]
    overwrite_pages = seed_pages[: min(16, len(seed_pages))]
    operations: list[dict[str, Any]] = []

    for index, page_id in enumerate(seed_pages):
        operations.append(make_full_page_op(profile, "write", index * base_gap, page_id, "setup"))

    read_start_time = len(seed_pages) * base_gap + settle_gap
    for index, page_id in enumerate(overwrite_pages[:8]):
        operations.append(make_full_page_op(profile, "read", read_start_time + index * base_gap, page_id, "baseline"))

    overwrite_start_time = read_start_time + len(overwrite_pages) * base_gap + settle_gap
    for index in range(measured_count):
        page_id = overwrite_pages[index % len(overwrite_pages)]
        operations.append(make_full_page_op(profile, "write", overwrite_start_time + index * base_gap, page_id, "overwrite"))

    final_read_start_time = overwrite_start_time + measured_count * base_gap + settle_gap
    for index, page_id in enumerate(overwrite_pages):
        operations.append(make_full_page_op(profile, "read", final_read_start_time + index * base_gap, page_id, "verify"))

    return GeneratedCase(
        name="overwrite_mapping",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        expect_media_reads=True,
        expect_user_programs_equal_writes=True,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
    )


def single_plane_page_ids(
    profile: Profile,
    count: int,
    *,
    channel: int = 0,
    chip: int = 0,
    die: int = 0,
    plane: int = 0,
) -> list[int]:
    return [
        cwdp_page_id(profile, channel=channel, chip=chip, die=die, plane=plane, cycle=cycle)
        for cycle in range(count)
    ]


def flashsim_mapping_reserved_blocks_per_plane(profile: Profile) -> int:
    plane_count = (
        profile.flash_channel_count
        * profile.chip_no_per_channel
        * profile.die_no_per_chip
        * profile.plane_no_per_die
    )
    total_random_access_pages = plane_count * profile.block_no_per_plane * profile.page_no_per_block
    reserved_blocks_per_plane = 1
    pages_per_reserved_block_cycle = plane_count * profile.page_no_per_block
    while True:
        reserved_pages = plane_count * reserved_blocks_per_plane * profile.page_no_per_block
        data_pages = total_random_access_pages - reserved_pages
        if data_pages <= 0:
            return reserved_blocks_per_plane
        mapping_pages = ceil_div(data_pages, FLASHSIM_MAPPING_ENTRIES_PER_PAGE)
        needed = max(1, ceil_div(mapping_pages, pages_per_reserved_block_cycle))
        if needed == reserved_blocks_per_plane:
            return reserved_blocks_per_plane
        reserved_blocks_per_plane = needed


def maintenance_gc_low_watermark(
    profile: Profile,
    fill_blocks: int,
    *,
    extra_allocated_blocks: int = 0,
) -> int:
    """Validation watermark that triggers after a bounded fill window."""
    reserved_blocks = flashsim_mapping_reserved_blocks_per_plane(profile)
    return max(1, profile.block_no_per_plane - reserved_blocks - fill_blocks - extra_allocated_blocks)


def maintenance_mqsim_gc_threshold(
    profile: Profile,
    fill_blocks: int,
    *,
    extra_allocated_blocks: int = 0,
) -> str:
    """MQSim GC threshold ratio matching maintenance_gc_low_watermark intent."""
    threshold_blocks = max(1, profile.block_no_per_plane - fill_blocks - extra_allocated_blocks + 1)
    threshold_ratio = min(0.999999, threshold_blocks / profile.block_no_per_plane)
    return f"{threshold_ratio:.6f}"


def generate_gc_pressure(
    profile: Profile,
    request_count: int,
    gap_ns: int,
    *,
    gc_rounds: int | None = None,
) -> GeneratedCase:
    """Deterministic full-page GC trace on one CWDP plane.

    Each round targets a different CWDP plane.  The round fills 49 blocks in
    that plane, overwrites five victim pages, and writes four fresh same-plane
    pages.  The ninth write in the round forces allocation of the next write-
    frontier block, so greedy GC should relocate the victim block's three still-
    valid pages before erasing it.  Spreading rounds across planes keeps each
    GC independent and avoids low-watermark carry-over from the previous round.
    """

    rounds = gc_rounds if gc_rounds is not None else max(1, request_count // 8)
    rounds = max(1, rounds)
    fill_blocks = 49
    plane_coordinates = [
        (channel, chip, die, plane)
        for channel in range(profile.flash_channel_count)
        for chip in range(profile.chip_no_per_channel)
        for die in range(profile.die_no_per_chip)
        for plane in range(profile.plane_no_per_die)
    ]
    max_rounds = len(plane_coordinates)
    if rounds > max_rounds:
        raise ValueError(f"gc_pressure supports at most {max_rounds} GC rounds")
    base_gap = max(gap_ns, profile.page_program_latency_lsb + 50_000)
    maintenance_gap = max(30_000_000, profile.block_erase_latency * 3)
    fill_pages = fill_blocks * profile.page_no_per_block
    fresh_pages_per_round = 4
    relocated_pages_per_gc = min(3, max(0, profile.page_no_per_block - 1))
    victim_invalidations = profile.page_no_per_block - relocated_pages_per_gc
    operations: list[dict[str, Any]] = []

    time_ns = 0
    verify_pages: list[int] = []
    for round_index in range(rounds):
        channel, chip, die, plane = plane_coordinates[round_index]
        target_pages = single_plane_page_ids(
            profile,
            fill_pages + fresh_pages_per_round + 4,
            channel=channel,
            chip=chip,
            die=die,
            plane=plane,
        )
        fill_target_pages = target_pages[:fill_pages]
        fresh_pages = target_pages[fill_pages : fill_pages + fresh_pages_per_round]
        for page_id in fill_target_pages:
            operations.append(make_full_page_op(profile, "write", time_ns, page_id, f"fill-r{round_index + 1}"))
            time_ns += base_gap

        time_ns += maintenance_gap
        victim_pages = fill_target_pages[: profile.page_no_per_block]
        round_writes = [*victim_pages[:victim_invalidations], *fresh_pages]
        for index, page_id in enumerate(round_writes):
            if index == profile.page_no_per_block:
                time_ns += maintenance_gap
            operations.append(make_full_page_op(profile, "write", time_ns, page_id, f"overwrite-r{round_index + 1}"))
            time_ns += base_gap
        verify_pages.extend(victim_pages)
        verify_pages.extend(fresh_pages)
        verify_pages.extend(fill_target_pages[-4:])
        time_ns += maintenance_gap

    for page_id in verify_pages:
        operations.append(make_full_page_op(profile, "read", time_ns, page_id, "verify"))
        time_ns += max(gap_ns, 1000)

    return GeneratedCase(
        name="gc_pressure",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        mqsim_gc_exec_threshold=maintenance_mqsim_gc_threshold(
            profile,
            fill_blocks,
            extra_allocated_blocks=1,
        ),
        mqsim_gc_block_selection_policy="GREEDY",
        mqsim_static_wl_threshold=1_000_000,
        expect_media_reads=False,
        expect_user_programs_equal_writes=False,
        expect_gc=True,
        expect_no_static_wl=True,
        expected_gc_count=rounds,
        expected_static_wl_count=0,
        expected_gc_relocated_pages=relocated_pages_per_gc * rounds,
        expected_erase_count=rounds,
        mqsim_maintenance_diagnostic=True,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        flashsim_runtime_overrides={
            "gc_low_watermark": maintenance_gc_low_watermark(
                profile,
                fill_blocks,
                extra_allocated_blocks=1,
            ),
            "stop_servicing_writes_threshold": 1,
            "gc_min_invalid_pages": victim_invalidations,
            "gc_emergency_watermark": 1,
            "gc_victim_policy": "greedy",
            "static_wl_wear_gap_threshold": 1_000_000,
        },
    )


def generate_hot_gc_backpressure(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    """Minimal hot-overwrite trace for the public-trace GC stall.

    The external Exchange 5k window stalls on normalized pages 18 and 19, which
    map to adjacent channels in the same chip/die/plane group under CWDP.  This
    trace keeps only that pressure pattern: a dense stream of overwrites to the
    two LPAs, with no artificial maintenance gap.  It therefore exercises the
    interaction between same-LPA write barriers, GC erase scheduling, and
    backpressure retry.
    """

    write_count = max(request_count, profile.page_no_per_block * 10 + 1)
    base_gap = max(gap_ns, 1000)
    hot_pages = [
        cwdp_page_id(profile, channel=2, chip=2, die=0, plane=0),
        cwdp_page_id(profile, channel=3, chip=2, die=0, plane=0),
    ]
    target_precondition_pages = [
        2322,
        2323,
        2706,
        2707,
        5010,
        5011,
        5778,
        5779,
    ]
    operations: list[dict[str, Any]] = []
    for index in range(write_count):
        page_id = hot_pages[index % len(hot_pages)]
        operations.append(make_full_page_op(profile, "write", index * base_gap, page_id, "hot-overwrite"))

    return GeneratedCase(
        name="hot_gc_backpressure",
        operations=operations,
        flash_precondition_records=[
            make_precondition_record(page_id) for page_id in target_precondition_pages
        ],
        mqsim_cache_mode="TURNED_OFF",
        mqsim_gc_exec_threshold=maintenance_mqsim_gc_threshold(
            profile,
            0,
            extra_allocated_blocks=4,
        ),
        mqsim_static_wl_threshold=1_000_000,
        expect_media_reads=False,
        expect_user_programs_equal_writes=False,
        expect_gc=True,
        expect_no_static_wl=True,
        mqsim_maintenance_diagnostic=True,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        flashsim_runtime_overrides={
            "gc_low_watermark": maintenance_gc_low_watermark(
                profile,
                0,
                extra_allocated_blocks=4,
            ),
            "stop_servicing_writes_threshold": 1,
            "gc_min_invalid_pages": max(4, profile.page_no_per_block // 2),
            "gc_emergency_watermark": 1,
            "gc_victim_policy": "greedy",
            "static_wl_wear_gap_threshold": 1_000_000,
        },
    )


def generate_wear_leveling(profile: Profile, request_count: int, gap_ns: int) -> GeneratedCase:
    """Deterministic static wear-leveling trace on one CWDP plane.

    The first eight overwrites fully invalidate block 0, and a ninth overwrite
    forces allocation of the next write-frontier block.  With static WL threshold
    set to 1 for this stress case, the newly erased block becomes the high-wear
    free destination, so static WL should relocate a cold valid block into it and
    erase the cold source block.  The verify window reads the overwritten hot
    pages and two cold blocks, including the block selected by the current
    Flash-Sim safe-candidate policy, so relocated data must remain readable.
    """

    del request_count
    base_gap = max(gap_ns, profile.page_program_latency_lsb + 50_000)
    maintenance_gap = max(40_000_000, profile.block_erase_latency * 4)
    fill_blocks = 48
    fill_pages = fill_blocks * profile.page_no_per_block
    target_pages = single_plane_page_ids(profile, fill_pages)
    hot_pages = target_pages[: profile.page_no_per_block]
    cold_pages = target_pages[profile.page_no_per_block : profile.page_no_per_block * 2]
    wl_source_probe_pages = target_pages[
        profile.page_no_per_block * 2 : profile.page_no_per_block * 3
    ]
    operations: list[dict[str, Any]] = []

    time_ns = 0
    for page_id in target_pages:
        operations.append(make_full_page_op(profile, "write", time_ns, page_id, "fill"))
        time_ns += base_gap

    time_ns += maintenance_gap
    for index, page_id in enumerate([*hot_pages, cold_pages[0]]):
        if index == profile.page_no_per_block:
            time_ns += maintenance_gap
        operations.append(make_full_page_op(profile, "write", time_ns, page_id, "overwrite"))
        time_ns += base_gap

    time_ns += maintenance_gap
    for page_id in [*hot_pages, *cold_pages, *wl_source_probe_pages]:
        operations.append(make_full_page_op(profile, "read", time_ns, page_id, "verify"))
        time_ns += max(gap_ns, 1000)

    return GeneratedCase(
        name="wear_leveling",
        operations=operations,
        mqsim_cache_mode="TURNED_OFF",
        mqsim_gc_exec_threshold=maintenance_mqsim_gc_threshold(
            profile,
            fill_blocks,
            extra_allocated_blocks=1,
        ),
        mqsim_gc_block_selection_policy="GREEDY",
        mqsim_static_wl_threshold=1,
        expect_media_reads=False,
        expect_user_programs_equal_writes=False,
        expect_gc=True,
        expect_static_wl=True,
        expected_gc_count=1,
        expected_static_wl_count=1,
        expected_gc_relocated_pages=profile.page_no_per_block,
        expected_erase_count=2,
        mqsim_maintenance_diagnostic=True,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        flashsim_runtime_overrides={
            "gc_low_watermark": maintenance_gc_low_watermark(
                profile,
                fill_blocks,
                extra_allocated_blocks=1,
            ),
            "stop_servicing_writes_threshold": 1,
            "gc_min_invalid_pages": profile.page_no_per_block,
            "gc_emergency_watermark": 1,
            "gc_victim_policy": "greedy",
            "static_wl_wear_gap_threshold": 1,
        },
    )


def sanitize_case_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return sanitized.strip("_") or "external_trace"


def profile_mqsim_logical_page_count(profile: Profile) -> int:
    sectors_per_plane = int(
        profile.block_no_per_plane
        * profile.page_no_per_block
        * profile.mqsim_sectors_per_page
        * (1.0 - MQSIM_OVERPROVISIONING_RATIO)
    )
    plane_count = (
        profile.flash_channel_count
        * profile.chip_no_per_channel
        * profile.die_no_per_chip
        * profile.plane_no_per_die
    )
    return (sectors_per_plane * plane_count) // profile.mqsim_sectors_per_page


def profile_flashsim_data_page_count(profile: Profile) -> int:
    random_access_pages = (
        profile.flash_channel_count
        * profile.chip_no_per_channel
        * profile.die_no_per_chip
        * profile.plane_no_per_die
        * profile.block_no_per_plane
        * profile.page_no_per_block
    )
    mapping_pages = ceil_div(random_access_pages, FLASHSIM_MAPPING_ENTRIES_PER_PAGE)
    return random_access_pages - mapping_pages


def external_logical_page_limit(profile: Profile) -> int:
    return min(profile_mqsim_logical_page_count(profile), profile_flashsim_data_page_count(profile))


def load_mqsim_trace_records(path: Path, max_records: int | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            fields = stripped.split()
            if len(fields) < 5:
                raise ValueError(f"{path}: line {line_no} has fewer than 5 columns")
            try:
                time_ns = int(fields[0])
                device = int(fields[1])
                start_sector = int(fields[2])
                size_sectors = int(fields[3])
                type_code = int(fields[4])
            except ValueError as exc:
                raise ValueError(f"{path}: line {line_no} has a non-integer field") from exc
            if type_code not in (0, 1):
                raise ValueError(f"{path}: line {line_no} has unsupported type {type_code}")
            records.append(
                {
                    "time": time_ns,
                    "device": device,
                    "start_sector": start_sector,
                    "size_sectors": size_sectors,
                    "type": "read" if type_code == 1 else "write",
                    "line_no": line_no,
                }
            )
            if max_records is not None and len(records) >= max_records:
                break
    return records


def load_flashsim_trace_records(path: Path, max_records: int | None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "commands" in data:
            data = data["commands"]
        elif "trace" in data:
            data = data["trace"]
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list or object containing commands/trace")
    records = data[:max_records] if max_records is not None else data
    normalized = []
    for index, record in enumerate(records):
        try:
            op_type = str(record["type"]).lower()
            time_ns = int(record["time"])
            start_lha = int(record["start_lha"])
            size = int(record["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: malformed Flash-Sim record at index {index}") from exc
        if op_type not in ("read", "write"):
            raise ValueError(f"{path}: record {index} has unsupported type {op_type}")
        normalized.append(
            {
                "type": op_type,
                "time": time_ns,
                "start_lha": start_lha,
                "size": size,
                "stream_id": int(record.get("stream_id", 0)),
            }
        )
    return normalized


def split_page_span(start_page: int, page_count: int, page_limit: int) -> list[tuple[int, int]]:
    if page_count <= 0:
        raise ValueError("external trace contains a non-positive page count")
    if page_count > page_limit:
        raise ValueError(
            f"external request spans {page_count} pages, exceeding logical page limit {page_limit}"
        )
    if start_page + page_count <= page_limit:
        return [(start_page, page_count)]
    first_count = page_limit - start_page
    return [(start_page, first_count), (0, page_count - first_count)]


def contiguous_page_spans(pages: list[int]) -> list[tuple[int, int]]:
    if not pages:
        raise ValueError("external request contains no mapped pages")
    spans: list[tuple[int, int]] = []
    span_start = pages[0]
    previous = pages[0]
    span_count = 1
    for page in pages[1:]:
        if page == previous + 1:
            span_count += 1
        else:
            spans.append((span_start, span_count))
            span_start = page
            span_count = 1
        previous = page
    spans.append((span_start, span_count))
    return spans


def operation_page_span(operation: dict[str, Any]) -> tuple[int, int]:
    start_page = operation["flashsim_start_lha"] // FLASHSIM_SECTORS_PER_PAGE
    page_count = operation["flashsim_size"] // FLASHSIM_SECTORS_PER_PAGE
    return start_page, page_count


def iter_operation_pages(operation: dict[str, Any]) -> range:
    start_page, page_count = operation_page_span(operation)
    return range(start_page, start_page + page_count)


def make_precondition_record(lpa: int) -> dict[str, Any]:
    return {
        "lpa": lpa,
        "valid_bitmap": [1] * FLASHSIM_SECTORS_PER_PAGE,
        "data": [((lpa * 1315423911) + sector) & 0xFFFF for sector in range(FLASHSIM_SECTORS_PER_PAGE)],
    }


def build_external_precondition_records(
    operations: list[dict[str, Any]],
    mode: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    if mode == "none":
        return None, {"mode": mode, "page_count": 0}

    pages: set[int] = set()
    if mode == "all-touched":
        for operation in operations:
            pages.update(iter_operation_pages(operation))
    elif mode == "read-before-write":
        written_pages: set[int] = set()
        for operation in operations:
            operation_pages = set(iter_operation_pages(operation))
            if operation["type"] == "read":
                pages.update(page for page in operation_pages if page not in written_pages)
            else:
                written_pages.update(operation_pages)
    elif mode == "read-pages":
        for operation in operations:
            if operation["type"] == "read":
                pages.update(iter_operation_pages(operation))
    else:
        raise ValueError(f"unsupported external precondition mode: {mode}")

    sorted_pages = sorted(pages)
    return [make_precondition_record(lpa) for lpa in sorted_pages], {
        "mode": mode,
        "page_count": len(sorted_pages),
        "min_lpa": sorted_pages[0] if sorted_pages else None,
        "max_lpa": sorted_pages[-1] if sorted_pages else None,
    }


def count_read_before_write(operations: list[dict[str, Any]]) -> dict[str, int]:
    written_pages: set[int] = set()
    request_count = 0
    page_ops = 0
    for operation in operations:
        pages = list(iter_operation_pages(operation))
        if operation["type"] == "read":
            missing_pages = [page for page in pages if page not in written_pages]
            if missing_pages:
                request_count += 1
                page_ops += len(missing_pages)
        else:
            written_pages.update(pages)
    return {
        "request_count": request_count,
        "page_ops": page_ops,
    }


def build_mqsim_warmup_operations(
    profile: Profile,
    precondition_records: list[dict[str, Any]] | None,
    warmup_gap_ns: int,
) -> list[dict[str, Any]]:
    if not precondition_records:
        return []

    pages = sorted({int(record["lpa"]) for record in precondition_records})
    operations: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        operations.append(
            {
                "type": "write",
                "time": index * warmup_gap_ns,
                "page_id": page,
                "flashsim_start_lha": page * FLASHSIM_SECTORS_PER_PAGE,
                "flashsim_size": FLASHSIM_SECTORS_PER_PAGE,
                "mqsim_start_sector": page * profile.mqsim_sectors_per_page,
                "mqsim_size_sectors": profile.mqsim_sectors_per_page,
                "phase": "mqsim_warmup",
                "mqsim_warmup_index": index,
            }
        )
    return operations


def mqsim_warmup_operations(case: GeneratedCase) -> list[dict[str, Any]]:
    return case.mqsim_warmup_operations or []


def mqsim_expected_counts(case: GeneratedCase) -> dict[str, int]:
    operations = case.operations
    warmup = mqsim_warmup_operations(case)
    return {
        "main_total": len(operations),
        "main_reads": sum(1 for item in operations if item["type"] == "read"),
        "main_writes": sum(1 for item in operations if item["type"] == "write"),
        "warmup_total": len(warmup),
        "warmup_reads": sum(1 for item in warmup if item["type"] == "read"),
        "warmup_writes": sum(1 for item in warmup if item["type"] == "write"),
        "trace_total": len(operations) + len(warmup),
        "trace_reads": sum(1 for item in operations if item["type"] == "read")
        + sum(1 for item in warmup if item["type"] == "read"),
        "trace_writes": sum(1 for item in operations if item["type"] == "write")
        + sum(1 for item in warmup if item["type"] == "write"),
    }


def build_external_trace_case(
    profile: Profile,
    flash_trace_path: Path,
    mqsim_trace_path: Path,
    *,
    name: str | None,
    max_requests: int | None,
    address_mode: str,
    precondition_mode: str,
    mqsim_preconditioning: bool,
    mqsim_initial_occupancy_percentage: int,
) -> GeneratedCase:
    flash_records = load_flashsim_trace_records(flash_trace_path, max_requests)
    mqsim_records = load_mqsim_trace_records(mqsim_trace_path, max_requests)
    if len(flash_records) != len(mqsim_records):
        raise ValueError(
            "external trace record count mismatch: "
            f"Flash-Sim={len(flash_records)}, MQSim={len(mqsim_records)}"
        )

    page_limit = external_logical_page_limit(profile)
    if page_limit <= 0:
        raise ValueError("computed non-positive logical page limit for external trace")

    operations: list[dict[str, Any]] = []
    read_count = 0
    write_count = 0
    original_page_ops = 0
    split_count = 0
    min_original_page: int | None = None
    max_original_page: int | None = None
    last_time: int | None = None
    compact_page_map: dict[int, int] = {}

    for index, (flash_record, mqsim_record) in enumerate(zip(flash_records, mqsim_records)):
        if flash_record["type"] != mqsim_record["type"]:
            raise ValueError(
                f"external trace type mismatch at index {index}: "
                f"Flash-Sim={flash_record['type']} MQSim={mqsim_record['type']}"
            )
        if flash_record["time"] != mqsim_record["time"]:
            raise ValueError(
                f"external trace time mismatch at index {index}: "
                f"Flash-Sim={flash_record['time']} MQSim={mqsim_record['time']}"
            )
        if last_time is not None and flash_record["time"] < last_time:
            raise ValueError(f"external trace time is not monotonic at index {index}")
        last_time = flash_record["time"]

        flash_start_bytes = flash_record["start_lha"] * FLASHSIM_SECTOR_SIZE_BYTES
        flash_size_bytes = flash_record["size"] * FLASHSIM_SECTOR_SIZE_BYTES
        mqsim_start_bytes = mqsim_record["start_sector"] * MQSIM_SECTOR_SIZE_BYTES
        mqsim_size_bytes = mqsim_record["size_sectors"] * MQSIM_SECTOR_SIZE_BYTES
        if flash_start_bytes != mqsim_start_bytes or flash_size_bytes != mqsim_size_bytes:
            raise ValueError(
                f"external trace address/size mismatch at index {index}: "
                f"Flash-Sim=({flash_start_bytes},{flash_size_bytes}) "
                f"MQSim=({mqsim_start_bytes},{mqsim_size_bytes})"
            )
        if flash_record["start_lha"] % FLASHSIM_SECTORS_PER_PAGE != 0:
            raise ValueError(f"external trace is not full-page aligned at index {index}")
        if flash_record["size"] % FLASHSIM_SECTORS_PER_PAGE != 0:
            raise ValueError(f"external trace contains a partial-page size at index {index}")

        original_page = flash_record["start_lha"] // FLASHSIM_SECTORS_PER_PAGE
        page_count = flash_record["size"] // FLASHSIM_SECTORS_PER_PAGE
        if page_count <= 0:
            raise ValueError(f"external trace has zero page count at index {index}")
        original_page_ops += page_count
        min_original_page = original_page if min_original_page is None else min(min_original_page, original_page)
        max_original_page = (
            original_page + page_count - 1
            if max_original_page is None
            else max(max_original_page, original_page + page_count - 1)
        )

        if address_mode == "raw":
            if original_page + page_count > page_limit:
                raise ValueError(
                    f"external raw LPA range [{original_page}, {original_page + page_count}) "
                    f"exceeds profile logical page limit {page_limit}; use --external-address-mode compact"
                )
            mapped_spans = [(original_page, page_count)]
        elif address_mode == "modulo":
            mapped_spans = split_page_span(original_page % page_limit, page_count, page_limit)
        elif address_mode == "compact":
            mapped_pages: list[int] = []
            for page_offset in range(page_count):
                source_page = original_page + page_offset
                mapped_page = compact_page_map.get(source_page)
                if mapped_page is None:
                    mapped_page = len(compact_page_map)
                    if mapped_page >= page_limit:
                        raise ValueError(
                            "external compact mapping exceeded profile logical page limit "
                            f"{page_limit}; reduce --external-max-requests or use a larger profile"
                        )
                    compact_page_map[source_page] = mapped_page
                mapped_pages.append(mapped_page)
            mapped_spans = contiguous_page_spans(mapped_pages)
        else:
            raise ValueError(f"unsupported external address mode: {address_mode}")

        if len(mapped_spans) > 1:
            split_count += 1

        op_type = flash_record["type"]
        if op_type == "read":
            read_count += 1
        else:
            write_count += 1
        for span_index, (mapped_page, mapped_count) in enumerate(mapped_spans):
            operations.append(
                {
                    "type": op_type,
                    "time": flash_record["time"],
                    "page_id": mapped_page,
                    "flashsim_start_lha": mapped_page * FLASHSIM_SECTORS_PER_PAGE,
                    "flashsim_size": mapped_count * FLASHSIM_SECTORS_PER_PAGE,
                    "mqsim_start_sector": mapped_page * profile.mqsim_sectors_per_page,
                    "mqsim_size_sectors": mapped_count * profile.mqsim_sectors_per_page,
                    "phase": "external",
                    "external_request_index": index,
                    "external_split_index": span_index,
                    "external_original_page": original_page,
                    "external_original_page_count": page_count,
                }
            )

    precondition_records, precondition_stats = build_external_precondition_records(
        operations,
        precondition_mode,
    )
    read_before_write_stats = count_read_before_write(operations)
    mqsim_warmup_gap_ns = max(1, profile.page_program_latency_lsb)
    mqsim_warmup_operations = (
        build_mqsim_warmup_operations(
            profile,
            precondition_records,
            mqsim_warmup_gap_ns,
        )
        if precondition_records is not None and not mqsim_preconditioning
        else []
    )
    mqsim_warmup_settle_ns = (
        max(
            1_000_000,
            profile.block_erase_latency * 2,
            profile.page_program_latency_lsb * profile.page_no_per_block * 2,
        )
        if mqsim_warmup_operations
        else 0
    )
    mqsim_main_time_shift_ns = (
        len(mqsim_warmup_operations) * mqsim_warmup_gap_ns + mqsim_warmup_settle_ns
    )
    touched_pages: set[int] = set()
    for operation in operations:
        touched_pages.update(iter_operation_pages(operation))

    external_name = sanitize_case_name(
        name or f"{flash_trace_path.stem}_{address_mode}_{len(flash_records)}"
    )
    stats = {
        "source_flash_trace": str(flash_trace_path),
        "source_mqsim_trace": str(mqsim_trace_path),
        "source_request_count": len(flash_records),
        "normalized_request_count": len(operations),
        "source_read_request_count": read_count,
        "source_write_request_count": write_count,
        "normalized_read_request_count": sum(1 for item in operations if item["type"] == "read"),
        "normalized_write_request_count": sum(1 for item in operations if item["type"] == "write"),
        "original_page_ops": original_page_ops,
        "normalized_page_ops": sum(operation_page_span(item)[1] for item in operations),
        "unique_normalized_pages": len(touched_pages),
        "address_mode": address_mode,
        "logical_page_limit": page_limit,
        "mqsim_logical_page_count": profile_mqsim_logical_page_count(profile),
        "flashsim_data_page_count": profile_flashsim_data_page_count(profile),
        "split_requests": split_count,
        "split_request_reason": (
            "logical-boundary"
            if address_mode == "modulo"
            else "noncontiguous-compact-mapping"
            if address_mode == "compact"
            else "none"
        ),
        "split_requests_at_logical_boundary": split_count,
        "compact_unique_source_pages": len(compact_page_map) if address_mode == "compact" else None,
        "min_original_page": min_original_page,
        "max_original_page": max_original_page,
        "precondition": precondition_stats,
        "read_before_write": read_before_write_stats,
        "mqsim_preconditioning": mqsim_preconditioning,
        "mqsim_initial_occupancy_percentage": (
            mqsim_initial_occupancy_percentage if mqsim_preconditioning else 0
        ),
        "mqsim_warmup_prefix_enabled": bool(mqsim_warmup_operations),
        "mqsim_warmup_request_count": len(mqsim_warmup_operations),
        "mqsim_warmup_page_ops": sum(operation_page_span(item)[1] for item in mqsim_warmup_operations),
        "mqsim_warmup_gap_ns": mqsim_warmup_gap_ns if mqsim_warmup_operations else 0,
        "mqsim_warmup_settle_ns": mqsim_warmup_settle_ns,
        "mqsim_main_time_shift_ns": mqsim_main_time_shift_ns,
        "mqsim_trace_request_count": len(operations) + len(mqsim_warmup_operations),
        "mqsim_trace_read_request_count": sum(1 for item in operations if item["type"] == "read")
        + sum(1 for item in mqsim_warmup_operations if item["type"] == "read"),
        "mqsim_trace_write_request_count": sum(1 for item in operations if item["type"] == "write")
        + sum(1 for item in mqsim_warmup_operations if item["type"] == "write"),
    }

    return GeneratedCase(
        name=external_name,
        operations=operations,
        flash_precondition_records=precondition_records,
        mqsim_warmup_operations=mqsim_warmup_operations or None,
        mqsim_main_time_shift_ns=mqsim_main_time_shift_ns,
        mqsim_enabled_preconditioning=mqsim_preconditioning,
        mqsim_initial_occupancy_percentage=(
            mqsim_initial_occupancy_percentage if mqsim_preconditioning else 0
        ),
        mqsim_cache_mode="TURNED_OFF",
        expect_media_reads=False,
        expect_user_programs_equal_writes=False,
        latency_alignment=True,
        direct_latency_compare=True,
        flashsim_cache_bypass=True,
        flashsim_plane_allocation="CWDP",
        trace_kind="external",
        external_trace_stats=stats,
    )


def write_case_inputs(case: GeneratedCase, profile: Profile, case_dir: Path) -> dict[str, Path]:
    case_dir.mkdir(parents=True, exist_ok=True)

    flash_trace_path = case_dir / f"validation_{profile.name}_{case.name}_flashsim_trace.json"
    flash_trace = [
        {
            "type": item["type"],
            "time": item["time"],
            "start_lha": item["flashsim_start_lha"],
            "size": item["flashsim_size"],
            "stream_id": 0,
        }
        for item in case.operations
    ]
    flash_trace_path.write_text(json.dumps(flash_trace, indent=2) + "\n", encoding="utf-8")

    mqsim_trace_path = case_dir / f"validation_{profile.name}_{case.name}_mqsim.trace"
    mqsim_lines = []
    type_map = {"write": 0, "read": 1}
    mqsim_trace_items: list[tuple[dict[str, Any], int]] = []
    for item in mqsim_warmup_operations(case):
        mqsim_trace_items.append((item, int(item["time"])))
    for item in case.operations:
        mqsim_trace_items.append((item, int(item["time"]) + case.mqsim_main_time_shift_ns))

    previous_time: int | None = None
    for item, mqsim_time in mqsim_trace_items:
        if previous_time is not None and mqsim_time < previous_time:
            raise ValueError(
                f"generated MQSim trace is not monotonic: {mqsim_time} < {previous_time}"
            )
        previous_time = mqsim_time
        mqsim_lines.append(
            " ".join(
                [
                    str(mqsim_time),
                    "0",
                    str(item["mqsim_start_sector"]),
                    str(item["mqsim_size_sectors"]),
                    str(type_map[item["type"]]),
                ]
            )
        )
    mqsim_trace_path.write_text("\n".join(mqsim_lines) + "\n", encoding="utf-8")

    precondition_path = None
    if case.flash_precondition_records is not None:
        precondition_path = case_dir / f"validation_{profile.name}_{case.name}_precondition.json"
        precondition_path.write_text(
            json.dumps(case.flash_precondition_records, indent=2) + "\n",
            encoding="utf-8",
        )

    flash_config_path = None
    if case.flashsim_runtime_overrides is not None:
        runtime = {
            "gc_low_watermark": 3,
            "stop_servicing_writes_threshold": 1,
            "gc_victim_policy": "greedy",
            "static_wl_enabled": True,
            "static_wl_wear_gap_threshold": 2,
            "cache_bypass": case.flashsim_cache_bypass,
            "plane_allocation_scheme": case.flashsim_plane_allocation or "PAGE_LEVEL",
        }
        runtime.update(case.flashsim_runtime_overrides)
        flash_config_path = case_dir / "flashsim_config.json"
        flash_config_path.write_text(
            json.dumps({"runtime": runtime}, indent=2) + "\n",
            encoding="utf-8",
        )

    ssd_config_path = case_dir / "mqsim_ssdconfig.xml"
    write_xml(
        ssd_config_path,
        build_ssd_config_xml(
            profile,
            enabled_preconditioning=case.mqsim_enabled_preconditioning,
            case=case,
        ),
    )

    workload_path = case_dir / "mqsim_workload.xml"
    write_xml(workload_path, build_workload_xml(profile, mqsim_trace_path, case))

    manifest_path = case_dir / "case_manifest.json"
    manifest = {
        "case": case.name,
        "trace_kind": case.trace_kind,
        "profile": asdict(profile),
        "operations": case.operations,
        "mqsim_warmup_request_count": len(mqsim_warmup_operations(case)),
        "mqsim_main_time_shift_ns": case.mqsim_main_time_shift_ns,
        "external_trace_stats": case.external_trace_stats,
        "notes": [
            "Flash-Sim sectors are 64 B; MQSim sectors are 512 B.",
            "This case is full-page aligned and avoids partial sector bitmaps.",
            "MQSim external traces may include a write warmup prefix to mirror Flash-Sim preconditioning.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    paths = {
        "flash_trace": flash_trace_path,
        "mqsim_trace": mqsim_trace_path,
        "ssd_config": ssd_config_path,
        "workload": workload_path,
        "manifest": manifest_path,
    }
    if precondition_path is not None:
        paths["precondition"] = precondition_path
    if flash_config_path is not None:
        paths["flash_config"] = flash_config_path
    return paths


def run_command(
    command: list[str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path | None = None,
    timeout: int = 120,
    stdin_devnull: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    if stderr_path is None:
        stderr_path = stdout_path
    stdin = subprocess.DEVNULL if stdin_devnull else None
    with stdout_path.open("w", encoding="utf-8") as out_handle:
        if stderr_path == stdout_path:
            return subprocess.run(
                command,
                cwd=str(cwd),
                text=True,
                stdout=out_handle,
                stderr=subprocess.STDOUT,
                stdin=stdin,
                timeout=timeout,
                env=env,
                check=False,
            )
        with stderr_path.open("w", encoding="utf-8") as err_handle:
            return subprocess.run(
                command,
                cwd=str(cwd),
                text=True,
                stdout=out_handle,
                stderr=err_handle,
                stdin=stdin,
                timeout=timeout,
                env=env,
                check=False,
            )


def choose_python(explicit_python: str | None) -> str:
    if explicit_python:
        return explicit_python
    venv_python = FLASH_SIM_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def ensure_mqsim_binary(mqsim_bin: Path, skip_build: bool, timeout: int) -> tuple[Path | None, list[str]]:
    notes: list[str] = []
    if mqsim_bin.exists():
        notes.append(f"Using existing MQSim binary: {mqsim_bin}")
        return mqsim_bin, notes

    if skip_build:
        notes.append(f"MQSim binary missing and --skip-build was set: {mqsim_bin}")
        return None, notes

    candidates = []
    env_cxx = os.environ.get("CXX")
    if env_cxx:
        candidates.append(env_cxx)
    candidates.extend(["g++16", "g++-16", "g++"])

    for compiler in candidates:
        compiler_path = shutil.which(compiler)
        if compiler_path is None:
            notes.append(f"Compiler not found: {compiler}")
            continue
        build_log = DEFAULT_OUTPUT_DIR / "_build" / "mqsim_build.log"
        result = run_command(
            ["make", f"CC={compiler_path}", f"LD={compiler_path}"],
            cwd=MQSIM_ROOT,
            stdout_path=build_log,
            timeout=timeout,
        )
        notes.append(f"Tried MQSim build with {compiler_path}: exit {result.returncode}")
        if result.returncode == 0 and mqsim_bin.exists():
            return mqsim_bin, notes

    return None, notes


def run_flashsim(
    paths: dict[str, Path],
    profile: Profile,
    python_bin: str,
    timeout: int,
    cache_bypass: bool = False,
    plane_allocation: str | None = None,
    no_timeline: bool = False,
    fast_report: bool = False,
) -> dict[str, Any]:
    events_path = paths["flash_trace"].with_name("flashsim_events.json")
    stdout_path = paths["flash_trace"].with_name("flashsim_stdout.log")
    command = [
        python_bin,
        "-m",
        "flash_sim.cli",
        "run-engine",
        str(paths["flash_trace"].resolve()),
    ]
    if "precondition" in paths:
        command.extend(["--pre-trace", str(paths["precondition"].resolve())])
    if "flash_config" in paths:
        command.extend(["--config", str(paths["flash_config"].resolve())])
    if cache_bypass:
        command.append("--cache-bypass")
    if plane_allocation:
        command.extend(["--plane-allocation", plane_allocation])
    if no_timeline:
        command.append("--no-timeline")
    if fast_report:
        command.append("--fast-report")
    command.append("--quiet")
    command.extend(
        [
            "--events",
            str(events_path.resolve()),
            "--no-viz",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(FLASH_SIM_ROOT)
    env.update(flashsim_event_runtime_env(profile))
    result = run_command(
        command,
        cwd=FLASH_SIM_ROOT,
        stdout_path=stdout_path,
        timeout=timeout,
        env=env,
    )
    report_path = FLASH_SIM_ROOT / "report" / f"{paths['flash_trace'].stem}_request_latency.json"
    return {
        "command": command,
        "exit_code": result.returncode,
        "stdout_log": str(stdout_path),
        "events": str(events_path),
        "report": str(report_path),
    }


def run_mqsim(paths: dict[str, Path], mqsim_bin: Path, timeout: int) -> dict[str, Any]:
    stdout_path = paths["workload"].with_name("mqsim_stdout.log")
    command = [
        str(mqsim_bin),
        "-i",
        str(paths["ssd_config"].resolve()),
        "-w",
        str(paths["workload"].resolve()),
    ]
    result = run_command(
        command,
        cwd=MQSIM_ROOT,
        stdout_path=stdout_path,
        timeout=timeout,
        stdin_devnull=True,
    )
    output_xml = paths["workload"].with_name(f"{paths['workload'].stem}_scenario_1.xml")
    return {
        "command": command,
        "exit_code": result.returncode,
        "stdout_log": str(stdout_path),
        "report": str(output_xml),
    }


def parse_flashsim_report(report_path: Path) -> dict[str, Any]:
    if not report_path.is_file():
        return {"exists": False}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    requests = data.get("requests", [])
    read_count = sum(1 for item in requests if item.get("type") == "READ")
    write_count = sum(1 for item in requests if item.get("type") == "WRITE")
    error_count = sum(1 for item in requests if item.get("status") == "ERROR")
    success_count = sum(1 for item in requests if item.get("status") == "SUCCESS")
    maintenance = data.get("meta", {}).get("maintenance", {})
    media_read_count = int_or_none(maintenance.get("physical_user_read_pages"))
    if media_read_count is None:
        media_read_count = count_flashsim_phy_array_ops(requests, "USER_READ")
    media_program_count = int_or_none(maintenance.get("physical_user_write_pages"))
    if media_program_count is None:
        media_program_count = count_flashsim_phy_array_ops(requests, "USER_WRITE")
    media_erase_count = int_or_none(maintenance.get("gc_erased_blocks"))
    if media_erase_count is None:
        media_erase_count = count_flashsim_phy_array_ops(requests, "GC_ERASE")
    read_requests = [item for item in requests if item.get("type") == "READ"]
    write_requests = [item for item in requests if item.get("type") == "WRITE"]
    host_latencies = [request_host_latency(item) for item in requests]
    read_host_latencies = [request_host_latency(item) for item in read_requests]
    write_host_latencies = [request_host_latency(item) for item in write_requests]
    non_success_requests = []
    for item in requests:
        if item.get("status") == "SUCCESS":
            continue
        lha_start = int_or_none(item.get("lha_start"))
        non_success_requests.append(
            {
                "trace_index": item.get("trace_index"),
                "type": item.get("type"),
                "page_id": lha_start // FLASHSIM_SECTORS_PER_PAGE if lha_start is not None else None,
                "status": item.get("status"),
                "error_message": item.get("error_message"),
            }
        )
    return {
        "exists": True,
        "request_count": data.get("meta", {}).get("request_count", len(requests)),
        "read_count": read_count,
        "write_count": write_count,
        "success_count": success_count,
        "error_count": error_count,
        "final_time": data.get("meta", {}).get("final_time"),
        "maintenance": maintenance,
        "media_read_count": media_read_count,
        "media_program_count": media_program_count,
        "media_gc_program_count": int_or_none(maintenance.get("physical_gc_write_pages")) or 0,
        "media_erase_count": media_erase_count,
        "avg_user_read_transaction_latency": average_flashsim_transaction_latency(
            requests,
            "USER_READ",
        ),
        "avg_user_write_transaction_latency": average_flashsim_transaction_latency(
            requests,
            "USER_WRITE",
        ),
        "avg_user_read_service_latency": average_flashsim_transaction_latency(
            requests,
            "USER_READ",
            stages=FLASHSIM_READ_SERVICE_STAGES,
        ),
        "avg_user_write_service_latency": average_flashsim_transaction_latency(
            requests,
            "USER_WRITE",
            stages=FLASHSIM_WRITE_SERVICE_STAGES,
        ),
        "avg_host_latency": average(
            host_latencies
        ),
        "avg_read_host_latency": average(
            read_host_latencies
        ),
        "avg_write_host_latency": average(
            write_host_latencies
        ),
        "host_latency_percentiles_ns": latency_percentiles(host_latencies),
        "read_host_latency_percentiles_ns": latency_percentiles(read_host_latencies),
        "write_host_latency_percentiles_ns": latency_percentiles(write_host_latencies),
        "avg_persistence_latency": average(
            item.get("persistence_total_latency", 0)
            for item in requests
        ),
        "non_success_count": len(non_success_requests),
        "non_success_requests": non_success_requests[:64],
        "data_cache_statuses": sorted({str(item.get("data_cache_status")) for item in requests}),
        "persistence_statuses": sorted({str(item.get("persistence_status")) for item in requests}),
    }


def request_host_latency(request: dict[str, Any]) -> Any:
    return request.get("host_total_latency", request.get("total_latency", 0))


def average_flashsim_transaction_latency(
    requests: list[dict[str, Any]],
    transaction_type: str,
    *,
    stages: tuple[str, ...] = FLASHSIM_TRANSACTION_LATENCY_STAGES,
) -> float:
    latencies = []
    for request in requests:
        total = 0
        for bucket_name in ("intervals", "persistence_intervals"):
            bucket = request.get(bucket_name, {})
            for stage in stages:
                for item in bucket.get(stage, []):
                    if item.get("transaction_type") != transaction_type:
                        continue
                    start = int_or_none(item.get("start"))
                    end = int_or_none(item.get("end"))
                    if start is not None and end is not None and end >= start:
                        total += end - start
        if total > 0:
            latencies.append(total)
    return average(latencies)


def count_flashsim_phy_array_ops(requests: list[dict[str, Any]], transaction_type: str) -> int:
    count = 0
    for request in requests:
        intervals = request.get("intervals", {}).get("phy_array_exec", [])
        persistence_intervals = request.get("persistence_intervals", {}).get("phy_array_exec", [])
        for item in [*intervals, *persistence_intervals]:
            if item.get("transaction_type") == transaction_type:
                count += 1
    return count


def parse_mqsim_stdout(stdout_path: Path) -> dict[str, Any]:
    if not stdout_path.exists():
        return {}
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"total requests generated:\s*(\d+)\s+total requests serviced:\s*(\d+)",
        text,
    )
    if not match:
        return {}
    return {
        "stdout_generated_request_count": int(match.group(1)),
        "stdout_serviced_request_count": int(match.group(2)),
    }


def parse_mqsim_report(report_path: Path, stdout_path: Path | None = None) -> dict[str, Any]:
    if not report_path.is_file():
        return {"exists": False}
    root = ET.parse(report_path).getroot()
    flows = []
    for flow in root.findall(".//Host.IO_Flow"):
        flow_data = {child.tag: parse_number(child.text) for child in list(flow)}
        for field in MQSIM_LATENCY_FIELDS_US:
            if field in flow_data and isinstance(flow_data[field], (int, float)):
                flow_data[f"{field}_us"] = flow_data[field]
                flow_data[f"{field}_ns"] = float(flow_data[field]) * 1000.0
        flows.append(flow_data)

    avg_device_response_time_us = average(
        flow.get("Device_Response_Time_us", flow.get("Device_Response_Time"))
        for flow in flows
    )
    avg_end_to_end_delay_us = average(
        flow.get("End_to_End_Request_Delay_us", flow.get("End_to_End_Request_Delay"))
        for flow in flows
    )
    min_device_response_time_us = min_float(
        flow.get("Min_Device_Response_Time_us", flow.get("Min_Device_Response_Time"))
        for flow in flows
    )
    max_device_response_time_us = max_float(
        flow.get("Max_Device_Response_Time_us", flow.get("Max_Device_Response_Time"))
        for flow in flows
    )
    min_end_to_end_delay_us = min_float(
        flow.get("Min_End_to_End_Request_Delay_us", flow.get("Min_End_to_End_Request_Delay"))
        for flow in flows
    )
    max_end_to_end_delay_us = max_float(
        flow.get("Max_End_to_End_Request_Delay_us", flow.get("Max_End_to_End_Request_Delay"))
        for flow in flows
    )
    streams = []
    for stream in root.findall(".//SSDDevice.IO_Stream"):
        stream_data = {child.tag: parse_number(child.text) for child in list(stream)}
        for field in (
            "Average_Read_Transaction_Turnaround_Time",
            "Average_Read_Transaction_Execution_Time",
            "Average_Read_Transaction_Transfer_Time",
            "Average_Read_Transaction_Waiting_Time",
            "Average_Write_Transaction_Turnaround_Time",
            "Average_Write_Transaction_Execution_Time",
            "Average_Write_Transaction_Transfer_Time",
            "Average_Write_Transaction_Waiting_Time",
        ):
            if field in stream_data and isinstance(stream_data[field], (int, float)):
                stream_data[f"{field}_us"] = stream_data[field]
                stream_data[f"{field}_ns"] = float(stream_data[field]) * 1000.0
        read_execution_ns = stream_data.get("Average_Read_Transaction_Execution_Time_ns")
        read_transfer_ns = stream_data.get("Average_Read_Transaction_Transfer_Time_ns")
        if read_execution_ns is not None and read_transfer_ns is not None:
            stream_data["Average_Read_Transaction_Service_Time_ns"] = (
                float(read_execution_ns) + float(read_transfer_ns)
            )
        write_execution_ns = stream_data.get("Average_Write_Transaction_Execution_Time_ns")
        write_transfer_ns = stream_data.get("Average_Write_Transaction_Transfer_Time_ns")
        if write_execution_ns is not None and write_transfer_ns is not None:
            stream_data["Average_Write_Transaction_Service_Time_ns"] = (
                float(write_execution_ns) + float(write_transfer_ns)
            )
        streams.append(stream_data)

    totals = {
        "request_count": sum_int(flow.get("Request_Count") for flow in flows),
        "read_count": sum_int(flow.get("Read_Request_Count") for flow in flows),
        "write_count": sum_int(flow.get("Write_Request_Count") for flow in flows),
        "bytes_transferred": sum_float(flow.get("Bytes_Transferred") for flow in flows),
        "avg_device_response_time_us": avg_device_response_time_us,
        "avg_device_response_time_ns": avg_device_response_time_us * 1000.0,
        "min_device_response_time_us": min_device_response_time_us,
        "min_device_response_time_ns": min_device_response_time_us * 1000.0,
        "max_device_response_time_us": max_device_response_time_us,
        "max_device_response_time_ns": max_device_response_time_us * 1000.0,
        "avg_end_to_end_delay_us": avg_end_to_end_delay_us,
        "avg_end_to_end_delay_ns": avg_end_to_end_delay_us * 1000.0,
        "min_end_to_end_delay_us": min_end_to_end_delay_us,
        "min_end_to_end_delay_ns": min_end_to_end_delay_us * 1000.0,
        "max_end_to_end_delay_us": max_end_to_end_delay_us,
        "max_end_to_end_delay_ns": max_end_to_end_delay_us * 1000.0,
        "avg_read_transaction_turnaround_time_us": average(
            stream.get("Average_Read_Transaction_Turnaround_Time_us")
            for stream in streams
        ),
        "avg_read_transaction_turnaround_time_ns": average(
            stream.get("Average_Read_Transaction_Turnaround_Time_ns")
            for stream in streams
        ),
        "avg_write_transaction_turnaround_time_us": average(
            stream.get("Average_Write_Transaction_Turnaround_Time_us")
            for stream in streams
        ),
        "avg_write_transaction_turnaround_time_ns": average(
            stream.get("Average_Write_Transaction_Turnaround_Time_ns")
            for stream in streams
        ),
        "avg_read_transaction_service_time_ns": average(
            stream.get("Average_Read_Transaction_Service_Time_ns")
            for stream in streams
        ),
        "avg_write_transaction_service_time_ns": average(
            stream.get("Average_Write_Transaction_Service_Time_ns")
            for stream in streams
        ),
        "latency_unit_in_mqsim_xml": "us",
    }
    if stdout_path is not None:
        totals.update(parse_mqsim_stdout(stdout_path))

    ftl_node = root.find(".//SSDDevice.FTL")
    ftl = dict(ftl_node.attrib) if ftl_node is not None else {}
    return {
        "exists": True,
        "flows": flows,
        "streams": streams,
        "totals": totals,
        "ftl": ftl,
    }


def parse_number(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return ""
    try:
        if any(marker in text for marker in (".", "e", "E")):
            number = float(text)
            if number.is_integer():
                return int(number)
            return number
        return int(text)
    except ValueError:
        return text


def sum_int(values: Any) -> int:
    total = 0
    for value in values:
        if value is None:
            continue
        total += int(float(value))
    return total


def sum_float(values: Any) -> float:
    total = 0.0
    for value in values:
        if value is None:
            continue
        total += float(value)
    return total


def average(values: Any) -> float:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def min_float(values: Any) -> float:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return 0.0
    return min(materialized)


def max_float(values: Any) -> float:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return 0.0
    return max(materialized)


def percentile(values: Any, percentile_value: float) -> float:
    materialized = sorted(float(value) for value in values if value is not None)
    if not materialized:
        return 0.0
    if len(materialized) == 1:
        return materialized[0]
    position = (len(materialized) - 1) * percentile_value / 100.0
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(materialized) - 1)
    fraction = position - lower_index
    return materialized[lower_index] * (1.0 - fraction) + materialized[upper_index] * fraction


def latency_percentiles(values: Any) -> dict[str, float]:
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def two_unit_duration(payload_bytes: int, channel_width_bytes: int, two_unit_time: int) -> int:
    return ceil_div(payload_bytes, channel_width_bytes * 2) * two_unit_time


def mqsim_nvddr2_read_command_duration(timing: Any, plane_count: int = 1) -> int:
    planes = max(1, min(int(plane_count), 4))
    return (
        timing.t_cs
        + planes * 6 * timing.t_wc
        + (planes - 1) * timing.t_dbsy
        + timing.t_wb
        + timing.t_rr
    )


def mqsim_nvddr2_program_command_duration(timing: Any, plane_count: int = 1) -> int:
    planes = max(1, min(int(plane_count), 4))
    final_plane = 6 * timing.t_wc + timing.t_adl + timing.t_wpst + timing.t_wpsth + timing.t_wb
    if planes == 1:
        return timing.t_cs + final_plane
    intermediate = 5 * timing.t_wc + timing.t_adl + timing.t_wpst + timing.t_cals + timing.t_wb
    return timing.t_cs + (planes - 1) * (intermediate + timing.t_dbsy) + final_plane


def mqsim_nvddr2_erase_command_duration(timing: Any, plane_count: int = 1) -> int:
    planes = max(1, min(int(plane_count), 4))
    per_plane = 4 * timing.t_wc + timing.t_wb
    return timing.t_cs + per_plane + (planes - 1) * (timing.t_dbsy + per_plane)


def mqsim_nvddr2_read_data_out_setup_duration(timing: Any, plane_count: int = 1) -> int:
    planes = max(1, min(int(plane_count), 4))
    base = timing.t_rpre + timing.t_dqsre
    if planes == 1:
        return base
    per_extra_plane = timing.t_rhw + 6 * timing.t_wc + timing.t_ccs + timing.t_rpre + timing.t_dqsre
    return base + (planes - 1) * per_extra_plane


def mqsim_nvddr2_data_in_duration(payload_bytes: int, timing: Any) -> int:
    return two_unit_duration(payload_bytes, timing.channel_width_bytes, timing.t_rc)


def mqsim_nvddr2_data_out_duration(payload_bytes: int, timing: Any, plane_count: int = 1) -> int:
    payload_time = two_unit_duration(payload_bytes, timing.channel_width_bytes, timing.t_dsc)
    if payload_time == 0:
        return 0
    return mqsim_nvddr2_read_data_out_setup_duration(timing, plane_count) + payload_time


def flashsim_onfi_sanity(profile: Profile) -> dict[str, Any]:
    sys.path.insert(0, str(FLASH_SIM_ROOT))
    from flash_sim import PHY
    from flash_sim.config import OnfiTimingConfig
    from flash_sim import common

    timing = OnfiTimingConfig(channel_width_bytes=profile.flash_channel_width)
    payload = profile.page_capacity
    expected = {
        "read_command": mqsim_nvddr2_read_command_duration(timing),
        "program_command": mqsim_nvddr2_program_command_duration(timing),
        "erase_command": mqsim_nvddr2_erase_command_duration(timing),
        "data_in": mqsim_nvddr2_data_in_duration(payload, timing),
        "data_out": mqsim_nvddr2_data_out_duration(payload, timing),
    }
    observed = {
        "read_command": PHY.onfi_read_command_duration(1, timing),
        "program_command": PHY.onfi_program_command_duration(1, timing),
        "erase_command": PHY.onfi_erase_command_duration(1, timing),
        "data_in": PHY.onfi_data_in_duration(payload, timing),
        "data_out": PHY.onfi_data_out_duration(payload, 1, timing),
    }
    mismatches = {
        key: {"expected": expected[key], "observed": observed[key]}
        for key in expected
        if expected[key] != observed[key]
    }
    timing_constants = {
        "flashsim_T_READ_LSB": common.T_READ_LSB,
        "flashsim_T_PROG": common.T_PROG,
        "flashsim_T_BERS": common.T_BERS,
        "profile_read": profile.page_read_latency_lsb,
        "profile_program": profile.page_program_latency_lsb,
        "profile_erase": profile.block_erase_latency,
    }
    timing_match = (
        common.T_READ_LSB == profile.page_read_latency_lsb
        and common.T_PROG == profile.page_program_latency_lsb
        and common.T_BERS == profile.block_erase_latency
    )
    return {
        "payload_bytes": payload,
        "channel_width_bytes": profile.flash_channel_width,
        "expected": expected,
        "observed": observed,
        "mismatches": mismatches,
        "timing_constants": timing_constants,
        "timing_match": timing_match,
    }


def mqsim_ftl_command_count(ftl: dict[str, Any], keys: list[str]) -> int:
    total = 0
    for key in keys:
        value = int_or_none(ftl.get(key))
        if value is not None:
            total += value
    return total


def mqsim_ftl_weighted_count(ftl: dict[str, Any], weights: dict[str, int]) -> int:
    total = 0
    for key, weight in weights.items():
        value = int_or_none(ftl.get(key))
        if value is not None:
            total += value * weight
    return total


def mqsim_effective_read_pages(ftl: dict[str, Any]) -> int:
    return mqsim_ftl_weighted_count(
        ftl,
        {
            "Issued_Flash_Read_CMD": 1,
            "Issued_Flash_Interleaved_Read_CMD": 2,
            "Issued_Flash_Multiplane_Read_CMD": 2,
            "Issued_Flash_Interleaved_Multiplane_Read_CMD": 4,
        },
    )


def mqsim_mapping_read_pages(ftl: dict[str, Any]) -> int:
    return int_or_none(ftl.get("Issued_Flash_Read_CMD_For_Mapping")) or 0


def mqsim_effective_user_read_pages(ftl: dict[str, Any]) -> int:
    return max(0, mqsim_effective_read_pages(ftl) - mqsim_mapping_read_pages(ftl))


def mqsim_effective_program_pages(ftl: dict[str, Any]) -> int:
    return mqsim_ftl_weighted_count(
        ftl,
        {
            "Issued_Flash_Program_CMD": 1,
            "Issued_Flash_Interleaved_Program_CMD": 2,
            "Issued_Flash_Multiplane_Program_CMD": 2,
            "Issued_Flash_Interleaved_Multiplane_Program_CMD": 4,
        },
    )


def mqsim_effective_erase_blocks(ftl: dict[str, Any]) -> int:
    return mqsim_ftl_weighted_count(
        ftl,
        {
            "Issued_Flash_Erase_CMD": 1,
            "Issued_Flash_Interleaved_Erase_CMD": 2,
            "Issued_Flash_Multiplane_Erase_CMD": 2,
            "Issued_Flash_Interleaved_Multiplane_Erase_CMD": 4,
        },
    )


def ratio_text(left: float, right: float) -> str:
    if left <= 0 or right <= 0:
        return "n/a"
    return f"{max(left, right) / min(left, right):.2f}"


def compare_results(
    case: GeneratedCase,
    profile: Profile,
    flashsim: dict[str, Any],
    mqsim: dict[str, Any],
    timing_sanity: dict[str, Any],
) -> dict[str, Any]:
    counts = mqsim_expected_counts(case)
    expected_count = counts["main_total"]
    expected_reads = counts["main_reads"]
    expected_writes = counts["main_writes"]
    expected_mqsim_count = counts["trace_total"]
    expected_mqsim_reads = counts["trace_reads"]
    expected_mqsim_writes = counts["trace_writes"]
    warmup_count = counts["warmup_total"]

    issues: list[str] = []
    notes: list[str] = []
    mqsim_completed_expected = False

    if not flashsim.get("exists"):
        issues.append("Flash-Sim request latency report was not produced.")
    else:
        if flashsim.get("request_count") != expected_count:
            issues.append(
                f"Flash-Sim request count {flashsim.get('request_count')} != expected {expected_count}."
            )
        if flashsim.get("read_count") != expected_reads:
            issues.append(
                f"Flash-Sim read count {flashsim.get('read_count')} != expected {expected_reads}."
            )
        if flashsim.get("write_count") != expected_writes:
            issues.append(
                f"Flash-Sim write count {flashsim.get('write_count')} != expected {expected_writes}."
            )
        if flashsim.get("success_count") != expected_count:
            issues.append(
                f"Flash-Sim success count {flashsim.get('success_count')} != expected {expected_count}."
            )
            pending = flashsim.get("non_success_requests") or []
            if pending:
                notes.append(f"Flash-Sim non-success requests: {pending[:16]}")
        if flashsim.get("error_count", 0) != 0:
            issues.append(f"Flash-Sim reported {flashsim.get('error_count')} request errors.")

    if not mqsim.get("exists"):
        issues.append("MQSim scenario report was not produced.")
    else:
        totals = mqsim.get("totals", {})
        ftl = mqsim.get("ftl", {})
        serviced_count = totals.get("stdout_serviced_request_count")
        mqsim_completed_expected = (
            serviced_count == expected_mqsim_count
            if serviced_count is not None
            else totals.get("request_count") == expected_mqsim_count
        )
        if warmup_count > 0:
            notes.append(
                "MQSim trace includes a preconditioning warmup prefix: "
                f"warmup_requests={warmup_count}, main_requests={expected_count}, "
                f"trace_requests={expected_mqsim_count}, "
                f"main_time_shift_ns={case.mqsim_main_time_shift_ns}. "
                "MQSim generated/serviced gates use the full trace count; "
                "Flash-Sim request gates use the main trace count."
            )
        if totals.get("request_count") != expected_mqsim_count:
            issues.append(
                "MQSim request count "
                f"{totals.get('request_count')} != expected trace count {expected_mqsim_count}."
            )
        if (
            serviced_count is not None
            and serviced_count != expected_mqsim_count
        ):
            message = (
                "MQSim serviced request count "
                f"{serviced_count} != expected trace count {expected_mqsim_count}."
            )
            notes.append(
                "MQSim did not service every generated request; latency and maintenance "
                "metrics from this run are treated as diagnostics, not as "
                "cross-simulator comparison evidence."
            )
            if case.trace_kind == "external" and warmup_count > 0:
                notes.append(message)
            else:
                issues.append(message)
        if totals.get("read_count") != expected_mqsim_reads:
            issues.append(
                f"MQSim read count {totals.get('read_count')} != expected {expected_mqsim_reads}."
            )
        if totals.get("write_count") != expected_mqsim_writes:
            issues.append(
                f"MQSim write count {totals.get('write_count')} != expected {expected_mqsim_writes}."
            )

    if case.expect_media_reads and flashsim.get("exists"):
        expected_media_reads = expected_reads
        flash_media_reads = flashsim.get("media_read_count")
        if flash_media_reads != expected_media_reads:
            issues.append(
                f"Flash-Sim media read count {flash_media_reads} != expected {expected_media_reads}."
            )
        if mqsim.get("exists") and mqsim_completed_expected:
            mqsim_ftl = mqsim.get("ftl", {})
            mqsim_raw_media_reads = mqsim_effective_read_pages(mqsim_ftl)
            mqsim_mapping_reads = mqsim_mapping_read_pages(mqsim_ftl)
            mqsim_media_reads = mqsim_effective_user_read_pages(mqsim_ftl)
            if mqsim_media_reads != expected_media_reads:
                issues.append(
                    "MQSim user media read command count "
                    f"{mqsim_media_reads} != expected {expected_media_reads} "
                    f"(raw={mqsim_raw_media_reads}, mapping={mqsim_mapping_reads})."
                )
        elif mqsim.get("exists"):
            notes.append("Skipped MQSim media-read gate because MQSim did not complete the expected trace.")

    if case.expect_user_programs_equal_writes and flashsim.get("exists"):
        flash_program_pages = flashsim.get("media_program_count")
        if flash_program_pages != expected_writes:
            issues.append(
                f"Flash-Sim media program count {flash_program_pages} != expected {expected_writes}."
            )
        if mqsim.get("exists") and mqsim_completed_expected:
            mqsim_program_pages = mqsim_effective_program_pages(mqsim.get("ftl", {}))
            if mqsim_program_pages != expected_mqsim_writes:
                issues.append(
                    "MQSim effective program page count "
                    f"{mqsim_program_pages} != expected trace writes {expected_mqsim_writes}."
                )
        elif mqsim.get("exists"):
            notes.append("Skipped MQSim media-program gate because MQSim did not complete the expected trace.")

    if flashsim.get("exists") and mqsim.get("exists"):
        flash_maintenance = flashsim.get("maintenance", {})
        mqsim_ftl = mqsim.get("ftl", {})
        flash_gc_count = int_or_none(flash_maintenance.get("gc_count")) or 0
        flash_wl_count = int_or_none(flash_maintenance.get("static_wl_count")) or 0
        flash_erases = int_or_none(flash_maintenance.get("gc_erased_blocks")) or 0
        flash_relocated_pages = int_or_none(flash_maintenance.get("gc_relocated_pages")) or 0
        mqsim_gc_count = int_or_none(mqsim_ftl.get("Total_GC_Executions")) or 0
        mqsim_wl_count = int_or_none(mqsim_ftl.get("Total_WL_Executions")) or 0
        mqsim_erases = mqsim_effective_erase_blocks(mqsim_ftl)
        mqsim_avg_gc_moves = float_or_none(mqsim_ftl.get("Average_Page_Movement_For_GC"))
        mqsim_relocated_pages = None
        if mqsim_avg_gc_moves is not None and mqsim_gc_count > 0:
            mqsim_relocated_pages = int(round(mqsim_avg_gc_moves * mqsim_gc_count))
        check_mqsim_maintenance = (
            not case.mqsim_maintenance_diagnostic and mqsim_completed_expected
        )

        if case.expect_gc:
            if flash_gc_count <= 0:
                issues.append("Flash-Sim did not report any GC executions.")
            if check_mqsim_maintenance and mqsim_gc_count <= 0:
                issues.append("MQSim did not report any GC executions.")
            if flash_erases <= 0:
                issues.append("Flash-Sim did not report any maintenance erases.")
            if check_mqsim_maintenance and mqsim_erases <= 0:
                issues.append("MQSim did not report any erase commands.")

        if case.expect_static_wl:
            if flash_wl_count <= 0:
                issues.append("Flash-Sim did not report any static WL executions.")
            if check_mqsim_maintenance and mqsim_wl_count <= 0:
                issues.append("MQSim did not report any static WL executions.")

        if case.expect_no_static_wl:
            if flash_wl_count != 0:
                issues.append(f"Flash-Sim reported unexpected static WL executions: {flash_wl_count}.")
            if check_mqsim_maintenance and mqsim_wl_count != 0:
                issues.append(f"MQSim reported unexpected static WL executions: {mqsim_wl_count}.")

        if case.expected_gc_count is not None:
            if flash_gc_count != case.expected_gc_count:
                issues.append(
                    f"Flash-Sim GC count {flash_gc_count} != expected {case.expected_gc_count}."
                )
            if check_mqsim_maintenance and mqsim_gc_count != case.expected_gc_count:
                issues.append(
                    f"MQSim GC count {mqsim_gc_count} != expected {case.expected_gc_count}."
                )

        if case.expected_static_wl_count is not None:
            if flash_wl_count != case.expected_static_wl_count:
                issues.append(
                    f"Flash-Sim static WL count {flash_wl_count} != expected {case.expected_static_wl_count}."
                )
            if check_mqsim_maintenance and mqsim_wl_count != case.expected_static_wl_count:
                issues.append(
                    f"MQSim static WL count {mqsim_wl_count} != expected {case.expected_static_wl_count}."
                )

        if case.expected_erase_count is not None:
            if flash_erases != case.expected_erase_count:
                issues.append(
                    f"Flash-Sim erase count {flash_erases} != expected {case.expected_erase_count}."
                )
            if check_mqsim_maintenance and mqsim_erases != case.expected_erase_count:
                issues.append(
                    f"MQSim erase count {mqsim_erases} != expected {case.expected_erase_count}."
                )

        if case.expected_gc_relocated_pages is not None:
            if flash_relocated_pages != case.expected_gc_relocated_pages:
                issues.append(
                    "Flash-Sim relocated page count "
                    f"{flash_relocated_pages} != expected {case.expected_gc_relocated_pages}."
                )
            if check_mqsim_maintenance and mqsim_relocated_pages != case.expected_gc_relocated_pages:
                issues.append(
                    "MQSim relocated page count "
                    f"{mqsim_relocated_pages} != expected {case.expected_gc_relocated_pages}."
                )

        if case.expect_gc or case.expect_static_wl:
            notes.append(
                "Maintenance diagnostic: "
                f"Flash-Sim gc={flash_gc_count}, static_wl={flash_wl_count}, "
                f"relocated_pages={flash_maintenance.get('gc_relocated_pages')}, "
                f"physical_gc_writes={flash_maintenance.get('physical_gc_write_pages')}, "
                f"erases={flash_erases}; "
                f"MQSim gc={mqsim_gc_count}, static_wl={mqsim_wl_count}, erases={mqsim_erases}, "
                f"relocated_pages={mqsim_relocated_pages}, "
                f"avg_gc_moves={mqsim_ftl.get('Average_Page_Movement_For_GC')}, "
                f"avg_wl_moves={mqsim_ftl.get('Average_Page_Movement_For_WL')}."
            )
            if case.mqsim_maintenance_diagnostic:
                notes.append(
                    "MQSim maintenance metrics are diagnostic for this case; "
                    "the pass/fail gate checks Flash-Sim's WL behavior."
                )
            if case.expect_static_wl:
                notes.append(
                    "MQSim's static-WL page-movement statistic is diagnostic only in this run; "
                    "the source code increments Total_WL_Executions, but the WL movement counter is not updated."
                )
                if mqsim_wl_count == 0:
                    notes.append(
                        "MQSim static WL did not start under the ideal-mapping validation profile. "
                        "One plausible implementation-level cause is that MQSim selects exactly one coldest "
                        "block for static WL; if that block is a reserved write frontier such as Translation_wf, "
                        "run_static_wearleveling returns instead of searching for the next safe cold block."
                    )

    if timing_sanity.get("mismatches"):
        issues.append(f"ONFI helper mismatch: {timing_sanity['mismatches']}")

    if not timing_sanity.get("timing_match"):
        message = (
            "Profile NAND array timings do not match Flash-Sim event-path constants: "
            f"{timing_sanity.get('timing_constants')}"
        )
        if profile.name == "flashsim-event":
            issues.append(message)
        else:
            notes.append(message)

    latency_comparable = (
        case.latency_alignment
        and flashsim.get("exists")
        and mqsim.get("exists")
        and mqsim_completed_expected
        and warmup_count == 0
    )
    if warmup_count > 0 and flashsim.get("exists") and mqsim.get("exists"):
        notes.append(
            "Direct MQSim latency/GC comparison is disabled for this run because "
            "MQSim aggregate XML metrics include the warmup prefix. Use the metrics "
            "as diagnostics only unless a main-phase-only MQSim report is added."
        )
    if latency_comparable:
        flash_read_latency = float(
            flashsim.get("avg_read_host_latency")
            or flashsim.get("avg_host_latency")
            or 0
        )
        mqsim_read_latency = float(
            mqsim.get("totals", {}).get("avg_read_transaction_turnaround_time_ns")
            or mqsim.get("totals", {}).get("avg_device_response_time_ns")
            or 0
        )
        flash_read_tx = float(flashsim.get("avg_user_read_transaction_latency") or 0)
        mqsim_read_tx = float(mqsim.get("totals", {}).get("avg_read_transaction_turnaround_time_ns") or 0)
        flash_write_tx = float(flashsim.get("avg_user_write_transaction_latency") or 0)
        mqsim_write_tx = float(mqsim.get("totals", {}).get("avg_write_transaction_turnaround_time_ns") or 0)
        flash_read_service = float(flashsim.get("avg_user_read_service_latency") or 0)
        mqsim_read_service = float(mqsim.get("totals", {}).get("avg_read_transaction_service_time_ns") or 0)
        flash_write_service = float(flashsim.get("avg_user_write_service_latency") or 0)
        mqsim_write_service = float(mqsim.get("totals", {}).get("avg_write_transaction_service_time_ns") or 0)
        if flash_read_latency > 0 and mqsim_read_latency > 0:
            ratio = max(flash_read_latency, mqsim_read_latency) / min(flash_read_latency, mqsim_read_latency)
            notes.append(
                f"Aligned read latency diagnostic: Flash-Sim read host avg={flash_read_latency:.1f} ns, "
                f"MQSim read transaction avg={mqsim_read_latency:.1f} ns, ratio={ratio:.2f}."
            )
            if ratio > 3.0:
                notes.append(
                    "Latency ratio is still large; inspect host-interface and scheduling model differences."
                )
        if case.direct_latency_compare:
            notes.append(
                "Direct media service latency comparison: "
                f"read Flash-Sim={flash_read_service:.1f} ns vs MQSim={mqsim_read_service:.1f} ns "
                f"(ratio={ratio_text(flash_read_service, mqsim_read_service)}), "
                f"write Flash-Sim={flash_write_service:.1f} ns vs MQSim={mqsim_write_service:.1f} ns "
                f"(ratio={ratio_text(flash_write_service, mqsim_write_service)})."
            )
            notes.append(
                "Direct media transaction latency comparison: "
                f"read Flash-Sim={flash_read_tx:.1f} ns vs MQSim={mqsim_read_tx:.1f} ns "
                f"(ratio={ratio_text(flash_read_tx, mqsim_read_tx)}), "
                f"write Flash-Sim={flash_write_tx:.1f} ns vs MQSim={mqsim_write_tx:.1f} ns "
                f"(ratio={ratio_text(flash_write_tx, mqsim_write_tx)})."
            )
    else:
        notes.append(
            "Latency equality is not asserted because Flash-Sim can complete writes at "
            "controller-cache acceptance while persistence is reported separately."
        )
    notes.append(
        "MQSim XML latency fields are reported in microseconds; the HTML report "
        "converts them to nanoseconds for the diagnostic latency snapshot."
    )
    if case.trace_kind == "external":
        stats = case.external_trace_stats or {}
        notes.append(
            "External validation trace is normalized before replay: "
            f"address_mode={stats.get('address_mode')}, "
            f"logical_page_limit={stats.get('logical_page_limit')}, "
            f"precondition={stats.get('precondition', {}).get('mode')}, "
            f"source_requests={stats.get('source_request_count')}, "
            f"normalized_requests={stats.get('normalized_request_count')}."
        )
    else:
        notes.append(
            "Generated validation traces avoid partial-sector bitmaps; bitmap/partial-page "
            "semantics should be a later validation layer."
        )

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "notes": notes,
    }


def load_dataset_manifest() -> dict[str, Any]:
    return {
        "paper_validation_real_traces": {
            "source": "MQSim FAST'18 Table 2 validation workloads",
            "names": ["tpcc", "tpce", "exchange"],
            "local_full_datasets_present": False,
        },
        "paper_reproducibility_inputs_present": [
            "MQSim/fast18/data-cache-contention",
            "MQSim/fast18/backend-contention",
            "MQSim/fast18/queue-fetch-size",
        ],
        "local_trace_samples_present": [
            "MQSim/traces/tpcc-small.trace",
            "MQSim/traces/wsrch-small.trace",
        ],
        "first_validation_scope": [
            "generated full-page aligned trace",
            "external full-page aligned public trace replay",
            "traditional flash read/write semantics",
            "no partial sector bitmap",
            "no modern SSD calibration yet",
        ],
    }


def write_summary(summary_path: Path, payload: dict[str, Any]) -> None:
    summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def html_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def status_class(status: str) -> str:
    normalized = status.lower()
    if normalized == "pass":
        return "pass"
    if normalized == "fail":
        return "fail"
    return "info"


def make_check_row(name: str, expected: Any, flash_value: Any, mqsim_value: Any, status: str) -> str:
    return (
        "<tr>"
        f"<td>{html_escape(name)}</td>"
        f"<td>{html_escape(format_metric(expected))}</td>"
        f"<td>{html_escape(format_metric(flash_value))}</td>"
        f"<td>{html_escape(format_metric(mqsim_value))}</td>"
        f"<td><span class=\"badge {status_class(status)}\">{html_escape(status)}</span></td>"
        "</tr>"
    )


def make_latency_row(name: str, flash_value: Any, mqsim_value: Any) -> str:
    flash_float = float(flash_value or 0)
    mqsim_float = float(mqsim_value or 0)
    ratio = ratio_text(flash_float, mqsim_float)
    return (
        "<tr>"
        f"<td>{html_escape(name)}</td>"
        f"<td>{html_escape(format_metric(flash_value))}</td>"
        f"<td>{html_escape(format_metric(mqsim_value))}</td>"
        f"<td>{html_escape(ratio)}</td>"
        "</tr>"
    )


def make_bar(label: str, value: Any, max_value: float) -> str:
    numeric = float(value or 0)
    width = 0 if max_value <= 0 else min(100, (numeric / max_value) * 100)
    return (
        "<div class=\"bar-row\">"
        f"<div class=\"bar-label\">{html_escape(label)}</div>"
        "<div class=\"bar-track\">"
        f"<div class=\"bar-fill\" style=\"width: {width:.2f}%\"></div>"
        "</div>"
        f"<div class=\"bar-value\">{html_escape(format_metric(value))}</div>"
        "</div>"
    )


def make_list(items: list[str], empty_text: str) -> str:
    if not items:
        return f"<p class=\"muted\">{html_escape(empty_text)}</p>"
    return "<ul>" + "".join(f"<li>{html_escape(item)}</li>" for item in items) + "</ul>"


def write_html_report(report_path: Path, payload: dict[str, Any]) -> None:
    profile = payload["profile"]
    case = payload["case"]
    comparison = payload["comparison"]
    flash = payload["reports"]["flashsim"]
    mqsim = payload["reports"]["mqsim"]
    timing = payload["timing_sanity"]
    mqsim_totals = mqsim.get("totals", {})
    flash_maintenance = flash.get("maintenance", {})
    mqsim_ftl = mqsim.get("ftl", {})

    expected_total = case["request_count"]
    expected_reads = case.get("read_count", 0)
    expected_writes = case.get("write_count", 0)
    expected_mqsim_total = case.get("mqsim_expected_request_count", expected_total)
    expected_mqsim_reads = case.get("mqsim_expected_read_count", expected_reads)
    expected_mqsim_writes = case.get("mqsim_expected_write_count", expected_writes)
    mqsim_warmup_count = case.get("mqsim_warmup_request_count", 0)
    mqsim_maintenance_diagnostic = bool(case.get("mqsim_maintenance_diagnostic"))

    flash_media_reads = flash.get("media_read_count")
    mqsim_raw_media_reads = mqsim_effective_read_pages(mqsim_ftl)
    mqsim_mapping_reads = mqsim_mapping_read_pages(mqsim_ftl)
    mqsim_media_reads = mqsim_effective_user_read_pages(mqsim_ftl)
    flash_programs = flash.get("media_program_count")
    flash_gc_programs = flash.get("media_gc_program_count")
    mqsim_programs = mqsim_effective_program_pages(mqsim_ftl)
    flash_erases = int_or_none(flash_maintenance.get("gc_erased_blocks"))
    mqsim_erases = mqsim_effective_erase_blocks(mqsim_ftl)
    flash_gc_count = int_or_none(flash_maintenance.get("gc_count")) or 0
    flash_wl_count = int_or_none(flash_maintenance.get("static_wl_count")) or 0
    flash_relocated_pages = int_or_none(flash_maintenance.get("gc_relocated_pages")) or 0
    mqsim_gc_count = int_or_none(mqsim_ftl.get("Total_GC_Executions")) or 0
    mqsim_wl_count = int_or_none(mqsim_ftl.get("Total_WL_Executions")) or 0
    mqsim_avg_gc_moves = float_or_none(mqsim_ftl.get("Average_Page_Movement_For_GC"))
    mqsim_relocated_pages = (
        int(round(mqsim_avg_gc_moves * mqsim_gc_count))
        if mqsim_avg_gc_moves is not None and mqsim_gc_count > 0
        else None
    )
    mqsim_avg_response_ns = mqsim_totals.get(
        "avg_device_response_time_ns",
        (mqsim_totals.get("avg_device_response_time") or 0) * 1000,
    )
    mqsim_avg_read_ns = mqsim_totals.get("avg_read_transaction_turnaround_time_ns") or mqsim_avg_response_ns
    mqsim_avg_write_ns = mqsim_totals.get("avg_write_transaction_turnaround_time_ns")
    mqsim_read_service_ns = mqsim_totals.get("avg_read_transaction_service_time_ns")
    mqsim_write_service_ns = mqsim_totals.get("avg_write_transaction_service_time_ns")
    flash_avg_read_ns = flash.get("avg_read_host_latency") or flash.get("avg_host_latency")
    flash_read_tx_ns = flash.get("avg_user_read_transaction_latency")
    flash_write_tx_ns = flash.get("avg_user_write_transaction_latency")
    flash_read_service_ns = flash.get("avg_user_read_service_latency")
    flash_write_service_ns = flash.get("avg_user_write_service_latency")
    flash_read_pctl = flash.get("read_host_latency_percentiles_ns", {})
    flash_write_pctl = flash.get("write_host_latency_percentiles_ns", {})

    checks = [
        (
            "Total requests",
            (
                f"Flash main={expected_total}; MQSim trace={expected_mqsim_total}"
                if expected_mqsim_total != expected_total
                else expected_total
            ),
            flash.get("request_count"),
            mqsim_totals.get("request_count"),
            "PASS"
            if flash.get("request_count") == expected_total
            and mqsim_totals.get("request_count") == expected_mqsim_total
            else "FAIL",
        ),
        (
            "Read requests",
            (
                f"Flash main={expected_reads}; MQSim trace={expected_mqsim_reads}"
                if expected_mqsim_reads != expected_reads
                else expected_reads
            ),
            flash.get("read_count"),
            mqsim_totals.get("read_count"),
            "PASS"
            if flash.get("read_count") == expected_reads
            and mqsim_totals.get("read_count") == expected_mqsim_reads
            else "FAIL",
        ),
        (
            "Write requests",
            (
                f"Flash main={expected_writes}; MQSim trace={expected_mqsim_writes}"
                if expected_mqsim_writes != expected_writes
                else expected_writes
            ),
            flash.get("write_count"),
            mqsim_totals.get("write_count"),
            "PASS"
            if flash.get("write_count") == expected_writes
            and mqsim_totals.get("write_count") == expected_mqsim_writes
            else "FAIL",
        ),
        (
            "Flash-Sim host-visible errors",
            0,
            flash.get("error_count"),
            "n/a",
            "PASS" if flash.get("error_count") == 0 else "FAIL",
        ),
        (
            "Flash-Sim successful completions",
            expected_total,
            flash.get("success_count"),
            "n/a",
            "PASS" if flash.get("success_count") == expected_total else "FAIL",
        ),
        (
            "ONFI helper formula",
            "no mismatch",
            "no mismatch" if not timing.get("mismatches") else timing.get("mismatches"),
            "MQSim source formula",
            "PASS" if not timing.get("mismatches") else "FAIL",
        ),
        (
            "Array timing constants",
            "profile match",
            "match" if timing.get("timing_match") else "different",
            "profile",
            "PASS" if timing.get("timing_match") else "INFO",
        ),
        (
            "User media read page ops",
            expected_reads if case.get("expect_media_reads") else "diagnostic",
            flash_media_reads,
            mqsim_media_reads,
            (
                "PASS"
                if case.get("expect_media_reads")
                and flash_media_reads == expected_reads
                and mqsim_media_reads == expected_reads
                else "INFO"
            ),
        ),
        (
            "MQSim raw read page ops",
            "diagnostic",
            "n/a",
            mqsim_raw_media_reads,
            "INFO",
        ),
        (
            "MQSim mapping read page ops",
            "diagnostic",
            "n/a",
            mqsim_mapping_reads,
            "INFO",
        ),
        (
            "Media program page ops",
            (
                f"Flash main={expected_writes}; MQSim trace={expected_mqsim_writes}"
                if expected_mqsim_writes != expected_writes
                else expected_writes
            ),
            flash_programs,
            mqsim_programs,
            (
                "PASS"
                if case.get("expect_user_programs_equal_writes", True)
                and flash_programs == expected_writes
                and mqsim_programs == expected_mqsim_writes
                else "INFO"
            ),
        ),
        (
            "GC/WL relocated program pages",
            case.get("expected_gc_relocated_pages", "diagnostic"),
            flash_relocated_pages if case.get("expected_gc_relocated_pages") is not None else flash_gc_programs,
            mqsim_relocated_pages if case.get("expected_gc_relocated_pages") is not None else "see MQSim GC/WL avg",
            (
                "PASS"
                if case.get("expected_gc_relocated_pages") is not None
                and flash_relocated_pages == case.get("expected_gc_relocated_pages")
                and (
                    mqsim_maintenance_diagnostic
                    or mqsim_relocated_pages == case.get("expected_gc_relocated_pages")
                )
                else "INFO"
            ),
        ),
        (
            "GC executions",
            case.get("expected_gc_count") if case.get("expected_gc_count") is not None else (">0" if case.get("expect_gc") else "diagnostic"),
            flash_gc_count,
            mqsim_gc_count,
            (
                "PASS"
                if (
                    case.get("expected_gc_count") is not None
                    and flash_gc_count == case.get("expected_gc_count")
                    and (
                        mqsim_maintenance_diagnostic
                        or mqsim_gc_count == case.get("expected_gc_count")
                    )
                )
                or (
                    case.get("expected_gc_count") is None
                    and case.get("expect_gc")
                    and flash_gc_count > 0
                    and (mqsim_maintenance_diagnostic or mqsim_gc_count > 0)
                )
                else ("FAIL" if case.get("expect_gc") else "INFO")
            ),
        ),
        (
            "Static WL executions",
            case.get("expected_static_wl_count") if case.get("expected_static_wl_count") is not None else (">0" if case.get("expect_static_wl") else (0 if case.get("expect_no_static_wl") else "diagnostic")),
            flash_wl_count,
            mqsim_wl_count,
            (
                "PASS"
                if (
                    case.get("expected_static_wl_count") is not None
                    and flash_wl_count == case.get("expected_static_wl_count")
                    and (
                        mqsim_maintenance_diagnostic
                        or mqsim_wl_count == case.get("expected_static_wl_count")
                    )
                )
                or (
                    case.get("expected_static_wl_count") is None
                    and
                    case.get("expect_static_wl")
                    and flash_wl_count > 0
                    and (mqsim_maintenance_diagnostic or mqsim_wl_count > 0)
                )
                or (
                    case.get("expected_static_wl_count") is None
                    and
                    case.get("expect_no_static_wl")
                    and flash_wl_count == 0
                    and (mqsim_maintenance_diagnostic or mqsim_wl_count == 0)
                )
                else ("FAIL" if case.get("expect_static_wl") or case.get("expect_no_static_wl") else "INFO")
            ),
        ),
        (
            "Erase commands",
            case.get("expected_erase_count") if case.get("expected_erase_count") is not None else (">0" if case.get("expect_gc") or case.get("expect_static_wl") else 0),
            flash_erases,
            mqsim_erases,
            (
                "PASS"
                if (
                    case.get("expected_erase_count") is not None
                    and flash_erases == case.get("expected_erase_count")
                    and (
                        mqsim_maintenance_diagnostic
                        or mqsim_erases == case.get("expected_erase_count")
                    )
                )
                or (
                    case.get("expected_erase_count") is None
                    and
                    (case.get("expect_gc") or case.get("expect_static_wl"))
                    and (flash_erases or 0) > 0
                    and (mqsim_maintenance_diagnostic or (mqsim_erases or 0) > 0)
                )
                or (
                    case.get("expected_erase_count") is None
                    and
                    not case.get("expect_gc")
                    and not case.get("expect_static_wl")
                    and flash_erases == 0
                    and (mqsim_maintenance_diagnostic or mqsim_erases == 0)
                )
                else ("FAIL" if case.get("expect_gc") or case.get("expect_static_wl") else "INFO")
            ),
        ),
    ]

    count_values = [
        expected_total,
        flash.get("request_count", 0),
        mqsim_totals.get("request_count", 0),
        expected_mqsim_total,
        flash.get("write_count", 0),
        mqsim_totals.get("write_count", 0),
        flash.get("read_count", 0),
        mqsim_totals.get("read_count", 0),
    ]
    max_count = max(float(value or 0) for value in count_values) if count_values else 1.0

    latency_values = [
        flash.get("avg_host_latency", 0),
        flash_avg_read_ns or 0,
        flash.get("avg_persistence_latency", 0),
        mqsim_avg_response_ns,
        mqsim_avg_read_ns,
        mqsim_avg_write_ns or 0,
        flash_read_tx_ns or 0,
        flash_write_tx_ns or 0,
        mqsim_read_service_ns or 0,
        mqsim_write_service_ns or 0,
        flash_read_service_ns or 0,
        flash_write_service_ns or 0,
        flash_read_pctl.get("p95", 0),
        flash_read_pctl.get("p99", 0),
        flash_write_pctl.get("p95", 0),
        flash_write_pctl.get("p99", 0),
    ]
    max_latency = max(float(value or 0) for value in latency_values) if latency_values else 1.0

    check_rows = "\n".join(make_check_row(*row) for row in checks)
    request_bars = "\n".join(
        [
            make_bar("Expected total", expected_total, max_count),
            make_bar("Expected MQSim trace total", expected_mqsim_total, max_count),
            make_bar("Flash-Sim total", flash.get("request_count"), max_count),
            make_bar("MQSim total", mqsim_totals.get("request_count"), max_count),
            make_bar("Flash-Sim writes", flash.get("write_count"), max_count),
            make_bar("MQSim writes", mqsim_totals.get("write_count"), max_count),
            make_bar("Flash-Sim reads", flash.get("read_count"), max_count),
            make_bar("MQSim reads", mqsim_totals.get("read_count"), max_count),
        ]
    )
    latency_bars = "\n".join(
        [
            make_bar("Flash-Sim host avg (ns)", flash.get("avg_host_latency"), max_latency),
            make_bar("Flash-Sim read host avg (ns)", flash_avg_read_ns, max_latency),
            make_bar("Flash-Sim read host p95 (ns)", flash_read_pctl.get("p95"), max_latency),
            make_bar("Flash-Sim read host p99 (ns)", flash_read_pctl.get("p99"), max_latency),
            make_bar("Flash-Sim read service avg (ns)", flash_read_service_ns, max_latency),
            make_bar("Flash-Sim write host p95 (ns)", flash_write_pctl.get("p95"), max_latency),
            make_bar("Flash-Sim write host p99 (ns)", flash_write_pctl.get("p99"), max_latency),
            make_bar("Flash-Sim write service avg (ns)", flash_write_service_ns, max_latency),
            make_bar("Flash-Sim persistence avg (ns)", flash.get("avg_persistence_latency"), max_latency),
            make_bar("MQSim device response avg (ns)", mqsim_avg_response_ns, max_latency),
            make_bar("MQSim read transaction avg (ns)", mqsim_avg_read_ns, max_latency),
            make_bar("MQSim write transaction avg (ns)", mqsim_avg_write_ns, max_latency),
            make_bar("MQSim read service avg (ns)", mqsim_read_service_ns, max_latency),
            make_bar("MQSim write service avg (ns)", mqsim_write_service_ns, max_latency),
        ]
    )
    latency_rows = "\n".join(
        [
            make_latency_row("Read media service latency (ns)", flash_read_service_ns, mqsim_read_service_ns),
            make_latency_row("Write media service latency (ns)", flash_write_service_ns, mqsim_write_service_ns),
            make_latency_row("Read host/device latency (ns)", flash_avg_read_ns, mqsim_avg_read_ns),
            make_latency_row("Read transaction turnaround incl. queue (ns)", flash_read_tx_ns, mqsim_avg_read_ns),
            make_latency_row("Write transaction turnaround incl. queue (ns)", flash_write_tx_ns, mqsim_avg_write_ns),
            make_latency_row("Overall host/device response (ns)", flash.get("avg_host_latency"), mqsim_avg_response_ns),
        ]
    )

    status = "PASS" if comparison.get("passed") else "FAIL"
    status_style = status_class(status)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flash-Sim vs MQSim Validation</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18202a;
      --muted: #657083;
      --line: #d8dee8;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --pass: #18794e;
      --pass-bg: #e9f7ef;
      --fail: #b42318;
      --fail-bg: #fdecec;
      --info: #8a5a00;
      --info-bg: #fff5d6;
      --accent: #246bfe;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(1120px, calc(100vw - 32px));
      margin: 24px auto 40px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-left: 8px solid var(--pass);
      padding: 20px;
      border-radius: 8px;
    }}
    .hero.fail {{ border-left-color: var(--fail); }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 6px; font-size: 24px; }}
    h2 {{ margin-bottom: 12px; font-size: 17px; }}
    .muted {{ color: var(--muted); }}
    .badge {{
      display: inline-block;
      min-width: 58px;
      text-align: center;
      padding: 4px 8px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 12px;
    }}
    .badge.pass {{ color: var(--pass); background: var(--pass-bg); }}
    .badge.fail {{ color: var(--fail); background: var(--fail-bg); }}
    .badge.info {{ color: var(--info); background: var(--info-bg); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin-top: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-top: 16px;
    }}
    .metric {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .big {{
      display: block;
      margin-top: 4px;
      font-size: 26px;
      font-weight: 750;
      color: var(--ink);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: 190px 1fr 90px;
      gap: 12px;
      align-items: center;
      margin: 10px 0;
    }}
    .bar-label {{ color: var(--muted); }}
    .bar-track {{
      height: 12px;
      background: #e9edf5;
      border-radius: 6px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--accent);
    }}
    .bar-value {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    code {{
      background: #eef2f8;
      padding: 2px 5px;
      border-radius: 4px;
    }}
    ul {{ margin-bottom: 0; padding-left: 20px; }}
    @media (max-width: 760px) {{
      .hero, .grid, .bar-row {{ grid-template-columns: 1fr; }}
      .bar-value {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero {status_style}">
      <div>
        <h1>Flash-Sim vs MQSim Validation</h1>
        <p class="muted">profile=<code>{html_escape(profile["name"])}</code> case=<code>{html_escape(case["name"])}</code></p>
      </div>
      <span class="badge {status_style}">{html_escape(status)}</span>
    </section>

    <section class="grid">
      <div class="panel"><span class="metric">Main requests</span><span class="big">{html_escape(expected_total)}</span></div>
      <div class="panel"><span class="metric">Flash-Sim errors</span><span class="big">{html_escape(flash.get("error_count", "n/a"))}</span></div>
      <div class="panel"><span class="metric">MQSim serviced / warmup</span><span class="big">{html_escape(mqsim_totals.get("stdout_serviced_request_count", mqsim_totals.get("request_count", "n/a")))}/{html_escape(mqsim_warmup_count)}</span></div>
    </section>

    <section class="panel">
      <h2>Correctness Gates</h2>
      <table>
        <thead><tr><th>Check</th><th>Expected</th><th>Flash-Sim</th><th>MQSim</th><th>Status</th></tr></thead>
        <tbody>{check_rows}</tbody>
      </table>
    </section>

    <section class="panel">
      <h2>Request Count View</h2>
      {request_bars}
    </section>

    <section class="panel">
      <h2>Direct Latency Comparison</h2>
      <p class="muted">Ratios compare the larger value to the smaller value. Media transaction rows are the most meaningful after SSD parameters are aligned.</p>
      <table>
        <thead><tr><th>Metric</th><th>Flash-Sim</th><th>MQSim</th><th>Ratio</th></tr></thead>
        <tbody>{latency_rows}</tbody>
      </table>
    </section>

    <section class="panel">
      <h2>Latency Snapshot</h2>
      <p class="muted">This section is diagnostic only. It is not a correctness gate because Flash-Sim write completion can be cache-visible while persistence is tracked separately. MQSim XML latency fields are reported in microseconds and converted to nanoseconds here.</p>
      {latency_bars}
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Issues</h2>
        {make_list(comparison.get("issues", []), "No correctness issues were reported.")}
      </div>
      <div class="panel">
        <h2>Notes</h2>
        {make_list(comparison.get("notes", []), "No notes.")}
      </div>
      <div class="panel">
        <h2>Artifacts</h2>
        <p class="muted">Summary JSON</p>
        <p><code>{html_escape(Path(payload["paths"]["summary"]).name)}</code></p>
        <p class="muted">MQSim report</p>
        <p><code>{html_escape(Path(payload["runs"]["mqsim"].get("report", "")).name)}</code></p>
      </div>
    </section>
  </main>
</body>
</html>
"""
    report_path.write_text(html_text, encoding="utf-8")


def ns_to_us(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) / 1000.0:.2f}"
    except (TypeError, ValueError):
        return "n/a"


def write_analysis_report(path: Path, payload: dict[str, Any]) -> None:
    profile = payload["profile"]
    case = payload["case"]
    comparison = payload["comparison"]
    flash = payload["reports"]["flashsim"]
    mqsim = payload["reports"]["mqsim"]
    mqsim_totals = mqsim.get("totals", {})
    mqsim_ftl = mqsim.get("ftl", {})
    flash_maintenance = flash.get("maintenance", {})
    external_stats = case.get("external_trace_stats") or {}
    flash_read_pctl = flash.get("read_host_latency_percentiles_ns", {})
    flash_write_pctl = flash.get("write_host_latency_percentiles_ns", {})
    expected_mqsim_count = case.get("mqsim_expected_request_count", case.get("request_count"))
    expected_mqsim_reads = case.get("mqsim_expected_read_count", case.get("read_count"))
    expected_mqsim_writes = case.get("mqsim_expected_write_count", case.get("write_count"))
    mqsim_warmup_count = case.get("mqsim_warmup_request_count", 0)

    lines = [
        "# External Trace Validation Analysis",
        "",
        f"- Profile: `{profile.get('name')}`",
        f"- Case: `{case.get('name')}`",
        f"- Status: `{'PASS' if comparison.get('passed') else 'FAIL'}`",
        f"- Main request count: expected={case.get('request_count')}, "
        f"Flash-Sim={flash.get('request_count')}",
        f"- MQSim trace request count: expected={expected_mqsim_count} "
        f"(warmup={mqsim_warmup_count}), MQSim={mqsim_totals.get('request_count')}, "
        f"serviced={mqsim_totals.get('stdout_serviced_request_count')}",
        f"- Main read/write requests: expected={case.get('read_count')}/{case.get('write_count')}, "
        f"Flash-Sim={flash.get('read_count')}/{flash.get('write_count')}, "
        f"MQSim trace expected={expected_mqsim_reads}/{expected_mqsim_writes}, "
        f"MQSim={mqsim_totals.get('read_count')}/{mqsim_totals.get('write_count')}",
        "",
    ]
    if external_stats:
        lines.extend(
            [
                "## Trace Normalization",
                "",
                f"- Source Flash trace: `{external_stats.get('source_flash_trace')}`",
                f"- Source MQSim trace: `{external_stats.get('source_mqsim_trace')}`",
                f"- Source requests: {external_stats.get('source_request_count')}",
                f"- Normalized requests: {external_stats.get('normalized_request_count')}",
                f"- Original page operations: {external_stats.get('original_page_ops')}",
                f"- Unique normalized pages: {external_stats.get('unique_normalized_pages')}",
                f"- Address mode: `{external_stats.get('address_mode')}`",
                f"- Compact unique source pages: {external_stats.get('compact_unique_source_pages')}",
                f"- Logical page limit: {external_stats.get('logical_page_limit')}",
                f"- Requests split: {external_stats.get('split_requests')} "
                f"({external_stats.get('split_request_reason')})",
                f"- Flash-Sim precondition: {external_stats.get('precondition')}",
                f"- Read-before-write in normalized main trace: {external_stats.get('read_before_write')}",
                f"- MQSim preconditioning: {external_stats.get('mqsim_preconditioning')}, "
                f"initial occupancy={external_stats.get('mqsim_initial_occupancy_percentage')}",
                f"- MQSim warmup prefix: enabled={external_stats.get('mqsim_warmup_prefix_enabled')}, "
                f"requests={external_stats.get('mqsim_warmup_request_count')}, "
                f"gap_ns={external_stats.get('mqsim_warmup_gap_ns')}, "
                f"main_time_shift_ns={external_stats.get('mqsim_main_time_shift_ns')}",
                "",
            ]
        )

    lines.extend(
        [
            "## Timing Snapshot",
            "",
            "| Metric | Flash-Sim (us) | MQSim (us) |",
            "| --- | ---: | ---: |",
            f"| Overall host/device avg | {ns_to_us(flash.get('avg_host_latency'))} | "
            f"{ns_to_us(mqsim_totals.get('avg_device_response_time_ns'))} |",
            f"| Read host/transaction avg | {ns_to_us(flash.get('avg_read_host_latency'))} | "
            f"{ns_to_us(mqsim_totals.get('avg_read_transaction_turnaround_time_ns'))} |",
            f"| Write host/transaction avg | {ns_to_us(flash.get('avg_write_host_latency'))} | "
            f"{ns_to_us(mqsim_totals.get('avg_write_transaction_turnaround_time_ns'))} |",
            f"| Read media service avg | {ns_to_us(flash.get('avg_user_read_service_latency'))} | "
            f"{ns_to_us(mqsim_totals.get('avg_read_transaction_service_time_ns'))} |",
            f"| Write media service avg | {ns_to_us(flash.get('avg_user_write_service_latency'))} | "
            f"{ns_to_us(mqsim_totals.get('avg_write_transaction_service_time_ns'))} |",
            f"| Flash-Sim read host p95 | {ns_to_us(flash_read_pctl.get('p95'))} | n/a |",
            f"| Flash-Sim read host p99 | {ns_to_us(flash_read_pctl.get('p99'))} | n/a |",
            f"| Flash-Sim write host p95 | {ns_to_us(flash_write_pctl.get('p95'))} | n/a |",
            f"| Flash-Sim write host p99 | {ns_to_us(flash_write_pctl.get('p99'))} | n/a |",
            "",
            "## Maintenance And Mapping Diagnostics",
            "",
            f"- Flash-Sim maintenance: {flash_maintenance}",
            f"- MQSim FTL GC/WL: gc={mqsim_ftl.get('Total_GC_Executions')}, "
            f"wl={mqsim_ftl.get('Total_WL_Executions')}, "
            f"mapping_reads={mqsim_ftl.get('Issued_Flash_Read_CMD_For_Mapping')}",
            f"- MQSim effective page commands: reads={mqsim_effective_read_pages(mqsim_ftl)}, "
            f"user_reads={mqsim_effective_user_read_pages(mqsim_ftl)}, "
            f"programs={mqsim_effective_program_pages(mqsim_ftl)}, "
            f"erases={mqsim_effective_erase_blocks(mqsim_ftl)}",
            "",
            "## Interpretation",
            "",
        ]
    )
    if comparison.get("passed"):
        lines.append(
            "The hard correctness gates passed: both simulators accepted and completed "
            "the same normalized full-page request stream, and Flash-Sim reported no "
            "host-visible request errors."
        )
    else:
        lines.append(
            "At least one hard correctness gate failed. Inspect the Issues section below "
            "before using latency differences as evidence."
        )
    lines.extend(
        [
            "",
            "Latency is diagnostic for this external run. The input is full-page aligned, "
            "but address normalization and preconditioning policy still make this a "
            "controlled replay rather than a vendor-calibrated SSD performance result.",
            "",
            "When an MQSim warmup prefix is enabled, MQSim aggregate latency and maintenance "
            "counters include the warmup phase; main-phase latency should not be directly "
            "compared until MQSim reports are split by phase.",
            "",
            "## Issues",
            "",
        ]
    )
    issues = comparison.get("issues") or []
    lines.extend(f"- {issue}" for issue in issues)
    if not issues:
        lines.append("- None")
    lines.extend(["", "## Notes", ""])
    notes = comparison.get("notes") or []
    lines.extend(f"- {note}" for note in notes)
    if not notes:
        lines.append("- None")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="flashsim-event-small")
    parser.add_argument(
        "--case",
        choices=[
            "write_stream",
            "flush_then_read",
            "rich_aligned",
            "parallel_cwdp",
            "overwrite_mapping",
            "gc_pressure",
            "hot_gc_backpressure",
            "wear_leveling",
        ],
        default="write_stream",
    )
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--gap-ns", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--python", dest="python_bin", default=None)
    parser.add_argument("--mqsim-bin", type=Path, default=MQSIM_ROOT / "MQSim")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-mqsim", action="store_true")
    parser.add_argument("--skip-flashsim", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--cache-bypass", action="store_true", help="Enable write-cache bypass in Flash-Sim (comparable to MQSim TURNED_OFF)")
    parser.add_argument("--plane-allocation", choices=["PAGE_LEVEL", "CWDP"], default=None, help="Plane allocation scheme for Flash-Sim (default: PAGE_LEVEL)")
    parser.add_argument("--flashsim-no-timeline", action="store_true", help="Disable Flash-Sim timeline recording for large validation runs")
    parser.add_argument("--flashsim-fast-report", action="store_true", help="Skip Flash-Sim detailed per-transaction interval reporting for large correctness replays")
    parser.add_argument("--flashsim-gc-low-watermark", type=int, default=None, help="Flash-Sim GC low watermark in blocks")
    parser.add_argument("--flashsim-stop-servicing-writes-threshold", type=int, default=None, help="Flash-Sim hard free-block reserve for host writes")
    parser.add_argument("--flashsim-gc-min-invalid-pages", type=int, default=None, help="Minimum invalid pages required for normal Flash-Sim GC victim selection")
    parser.add_argument("--flashsim-gc-emergency-watermark", type=int, default=None, help="Eligible free-block watermark where Flash-Sim GC bypasses the normal victim yield threshold")
    parser.add_argument("--flashsim-static-wl", choices=["on", "off"], default=None, help="Enable or disable Flash-Sim static wear leveling")
    parser.add_argument("--flashsim-static-wl-threshold", type=int, default=None, help="Flash-Sim static wear-leveling wear-gap threshold")
    parser.add_argument("--gc-rounds", type=int, default=None, help="Number of deterministic GC rounds for gc_pressure")
    parser.add_argument("--external-flash-trace", type=Path, default=None, help="Existing full-page Flash-Sim JSON trace to replay")
    parser.add_argument("--external-mqsim-trace", type=Path, default=None, help="Existing MQSim ASCII trace matching --external-flash-trace")
    parser.add_argument("--external-name", default=None, help="Case name for external trace artifacts")
    parser.add_argument("--external-max-requests", type=int, default=None, help="Limit source requests loaded from each external trace")
    parser.add_argument(
        "--external-address-mode",
        choices=["compact", "modulo", "raw"],
        default="compact",
        help="Normalize external addresses to the compact validation SSD",
    )
    parser.add_argument(
        "--external-precondition",
        choices=["none", "read-before-write", "read-pages", "all-touched"],
        default="read-pages",
        help="Flash-Sim precondition policy for external traces",
    )
    parser.add_argument(
        "--external-mqsim-preconditioning",
        action="store_true",
        help="Enable MQSim built-in preconditioning for external traces",
    )
    parser.add_argument(
        "--external-mqsim-initial-occupancy",
        type=int,
        default=0,
        help="MQSim Initial_Occupancy_Percentage when --external-mqsim-preconditioning is set",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = PROFILES[args.profile]

    if args.requests <= 0:
        raise SystemExit("--requests must be positive")
    if args.gap_ns < 0:
        raise SystemExit("--gap-ns must be non-negative")
    if args.gc_rounds is not None and args.gc_rounds <= 0:
        raise SystemExit("--gc-rounds must be positive")
    if args.external_max_requests is not None and args.external_max_requests <= 0:
        raise SystemExit("--external-max-requests must be positive")
    if not 0 <= args.external_mqsim_initial_occupancy <= 100:
        raise SystemExit("--external-mqsim-initial-occupancy must be in [0, 100]")
    external_mode = args.external_flash_trace is not None or args.external_mqsim_trace is not None
    if external_mode and (args.external_flash_trace is None or args.external_mqsim_trace is None):
        raise SystemExit("--external-flash-trace and --external-mqsim-trace must be provided together")

    if external_mode:
        case = build_external_trace_case(
            profile,
            args.external_flash_trace,
            args.external_mqsim_trace,
            name=args.external_name,
            max_requests=args.external_max_requests,
            address_mode=args.external_address_mode,
            precondition_mode=args.external_precondition,
            mqsim_preconditioning=args.external_mqsim_preconditioning,
            mqsim_initial_occupancy_percentage=args.external_mqsim_initial_occupancy,
        )
    elif args.case == "write_stream":
        case = generate_write_stream(profile, args.requests, args.gap_ns)
    elif args.case == "flush_then_read":
        case = generate_flush_then_read(profile, args.requests, args.gap_ns)
    elif args.case == "rich_aligned":
        case = generate_rich_aligned(profile, args.requests, args.gap_ns)
    elif args.case == "parallel_cwdp":
        case = generate_parallel_cwdp(profile, args.requests, args.gap_ns)
    elif args.case == "overwrite_mapping":
        case = generate_overwrite_mapping(profile, args.requests, args.gap_ns)
    elif args.case == "gc_pressure":
        case = generate_gc_pressure(profile, args.requests, args.gap_ns, gc_rounds=args.gc_rounds)
    elif args.case == "hot_gc_backpressure":
        case = generate_hot_gc_backpressure(profile, args.requests, args.gap_ns)
    elif args.case == "wear_leveling":
        case = generate_wear_leveling(profile, args.requests, args.gap_ns)
    else:
        raise SystemExit(f"unsupported case: {args.case}")

    runtime_overrides = dict(case.flashsim_runtime_overrides or {})
    if args.flashsim_gc_low_watermark is not None:
        if args.flashsim_gc_low_watermark < 0:
            raise SystemExit("--flashsim-gc-low-watermark must be non-negative")
        runtime_overrides["gc_low_watermark"] = args.flashsim_gc_low_watermark
    if args.flashsim_stop_servicing_writes_threshold is not None:
        if args.flashsim_stop_servicing_writes_threshold < 0:
            raise SystemExit("--flashsim-stop-servicing-writes-threshold must be non-negative")
        runtime_overrides["stop_servicing_writes_threshold"] = (
            args.flashsim_stop_servicing_writes_threshold
        )
    if args.flashsim_gc_min_invalid_pages is not None:
        if args.flashsim_gc_min_invalid_pages < 0:
            raise SystemExit("--flashsim-gc-min-invalid-pages must be non-negative")
        runtime_overrides["gc_min_invalid_pages"] = args.flashsim_gc_min_invalid_pages
    if args.flashsim_gc_emergency_watermark is not None:
        if args.flashsim_gc_emergency_watermark < 0:
            raise SystemExit("--flashsim-gc-emergency-watermark must be non-negative")
        runtime_overrides["gc_emergency_watermark"] = args.flashsim_gc_emergency_watermark
    if args.flashsim_static_wl is not None:
        runtime_overrides["static_wl_enabled"] = args.flashsim_static_wl == "on"
    if args.flashsim_static_wl_threshold is not None:
        if args.flashsim_static_wl_threshold < 0:
            raise SystemExit("--flashsim-static-wl-threshold must be non-negative")
        runtime_overrides["static_wl_wear_gap_threshold"] = args.flashsim_static_wl_threshold
    if runtime_overrides:
        case = replace(case, flashsim_runtime_overrides=runtime_overrides)

    case_dir = args.output_dir / profile.name / case.name
    paths = write_case_inputs(case, profile, case_dir)

    build_notes: list[str] = []
    flashsim_run: dict[str, Any] = {"skipped": True}
    mqsim_run: dict[str, Any] = {"skipped": True}

    if not args.skip_flashsim:
        python_bin = choose_python(args.python_bin)
        flashsim_run = run_flashsim(
            paths,
            profile,
            python_bin,
            args.timeout,
            cache_bypass=args.cache_bypass or case.flashsim_cache_bypass,
            plane_allocation=args.plane_allocation or case.flashsim_plane_allocation,
            no_timeline=args.flashsim_no_timeline,
            fast_report=args.flashsim_fast_report,
        )

    mqsim_bin = args.mqsim_bin
    if not args.skip_mqsim:
        resolved_mqsim_bin, build_notes = ensure_mqsim_binary(mqsim_bin, args.skip_build, args.timeout)
        if resolved_mqsim_bin is None:
            mqsim_run = {
                "skipped": False,
                "exit_code": None,
                "error": "MQSim binary is unavailable.",
            }
        else:
            mqsim_run = run_mqsim(paths, resolved_mqsim_bin, args.timeout)

    flashsim_report = parse_flashsim_report(Path(flashsim_run.get("report", "")))
    mqsim_report = parse_mqsim_report(
        Path(mqsim_run.get("report", "")),
        Path(mqsim_run.get("stdout_log", "")) if mqsim_run.get("stdout_log") else None,
    )
    timing_sanity = flashsim_onfi_sanity(profile)
    comparison = compare_results(case, profile, flashsim_report, mqsim_report, timing_sanity)

    if flashsim_run.get("exit_code") not in (0, None):
        comparison["issues"].append(f"Flash-Sim exited with {flashsim_run.get('exit_code')}.")
        comparison["passed"] = False
    if mqsim_run.get("exit_code") not in (0, None):
        comparison["issues"].append(f"MQSim exited with {mqsim_run.get('exit_code')}.")
        comparison["passed"] = False

    summary_path = case_dir / "summary.json"
    html_report_path = case_dir / "report.html"
    analysis_path = case_dir / "analysis.md"

    payload = {
        "profile": asdict(profile),
        "case": {
            "name": case.name,
            "trace_kind": case.trace_kind,
            "request_count": len(case.operations),
            "read_count": sum(1 for item in case.operations if item["type"] == "read"),
            "write_count": sum(1 for item in case.operations if item["type"] == "write"),
            "mqsim_warmup_request_count": mqsim_expected_counts(case)["warmup_total"],
            "mqsim_warmup_read_count": mqsim_expected_counts(case)["warmup_reads"],
            "mqsim_warmup_write_count": mqsim_expected_counts(case)["warmup_writes"],
            "mqsim_expected_request_count": mqsim_expected_counts(case)["trace_total"],
            "mqsim_expected_read_count": mqsim_expected_counts(case)["trace_reads"],
            "mqsim_expected_write_count": mqsim_expected_counts(case)["trace_writes"],
            "mqsim_main_time_shift_ns": case.mqsim_main_time_shift_ns,
            "expect_media_reads": case.expect_media_reads,
            "expect_user_programs_equal_writes": case.expect_user_programs_equal_writes,
            "latency_alignment": case.latency_alignment,
            "direct_latency_compare": case.direct_latency_compare,
            "mqsim_cache_mode": case.mqsim_cache_mode,
            "mqsim_initial_occupancy_percentage": case.mqsim_initial_occupancy_percentage,
            "mqsim_gc_exec_threshold": case.mqsim_gc_exec_threshold,
            "mqsim_gc_block_selection_policy": case.mqsim_gc_block_selection_policy,
            "mqsim_static_wl_threshold": case.mqsim_static_wl_threshold,
            "expect_gc": case.expect_gc,
            "expect_static_wl": case.expect_static_wl,
            "expect_no_static_wl": case.expect_no_static_wl,
            "expected_gc_count": case.expected_gc_count,
            "expected_static_wl_count": case.expected_static_wl_count,
            "expected_gc_relocated_pages": case.expected_gc_relocated_pages,
            "expected_erase_count": case.expected_erase_count,
            "mqsim_maintenance_diagnostic": case.mqsim_maintenance_diagnostic,
            "flashsim_cache_bypass": case.flashsim_cache_bypass,
            "flashsim_plane_allocation": case.flashsim_plane_allocation,
            "flashsim_runtime_overrides": case.flashsim_runtime_overrides,
            "external_trace_stats": case.external_trace_stats,
        },
        "dataset_manifest": load_dataset_manifest(),
        "paths": {
            **{key: str(value) for key, value in paths.items()},
            "summary": str(summary_path),
            "html_report": str(html_report_path),
            "analysis": str(analysis_path),
        },
        "build_notes": build_notes,
        "runs": {
            "flashsim": flashsim_run,
            "mqsim": mqsim_run,
        },
        "reports": {
            "flashsim": flashsim_report,
            "mqsim": mqsim_report,
        },
        "timing_sanity": timing_sanity,
        "comparison": comparison,
    }
    write_summary(summary_path, payload)
    write_html_report(html_report_path, payload)
    write_analysis_report(analysis_path, payload)

    status = "PASS" if comparison["passed"] else "FAIL"
    print(f"[{status}] profile={profile.name} case={case.name}")
    print(f"summary: {summary_path}")
    print(f"html: {html_report_path}")
    print(f"analysis: {analysis_path}")
    if comparison["issues"]:
        print("issues:")
        for issue in comparison["issues"]:
            print(f"  - {issue}")
    if comparison["notes"]:
        print("notes:")
        for note in comparison["notes"]:
            print(f"  - {note}")

    return 0 if comparison["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
