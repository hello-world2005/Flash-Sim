# -*- coding: utf-8 -*-
from dataclasses import dataclass
from collections import defaultdict
from dataclasses import field
from typing import Any
import math
import random
from .common import *
from .config import RuntimeConfig, make_event_runtime_geometry
from .PHY import PHY, PageType
from . import utils

PlaneKey = tuple[int, int, int, int]

class CMT:
    def __init__(self):
        self.cache: dict[int, cmt_entry] = {}
        self.lru_list: list[int] = []
        self.address_mapping_unit: Address_Mapping_Unit

    def is_cached(self, lpa: int) -> bool:
        return lpa in self.cache

    def get_cached_entry(self, lpa: int) -> cmt_entry:
        self.lru_list.remove(lpa)
        self.lru_list.insert(0, lpa)
        return self.cache[lpa]

    def update_entry(self, lpa: int, address: FlashAddress, dirty: bool) -> FlashAddress:
        entry = self.cache[lpa]
        invalidation_victim_address = entry.address
        plane_bke = self.address_mapping_unit.block_manager.block_keeping_book[
            invalidation_victim_address.channel
        ][invalidation_victim_address.chip][invalidation_victim_address.die][
            invalidation_victim_address.plane
        ]
        bke = plane_bke.block_entries[invalidation_victim_address.sub_plane]
        if dirty and invalidation_victim_address.page in bke.valid_pages:
            self.address_mapping_unit.block_manager._mark_invalid(
                invalidation_victim_address
            )
        entry.address = address
        if not QUIET:
            debug_info(f"[CMT] <add_entry> updating entry: ({lpa}, {repr(entry)})")
        entry.dirty = dirty
        self.lru_list.remove(lpa)
        self.lru_list.insert(0, lpa)
        return invalidation_victim_address
    
    def eject_entry(self, lpa: int):
        self.lru_list.remove(lpa)
        leaving_entry = self.cache.pop(lpa)
        self.address_mapping_unit.gmt[lpa] = leaving_entry
        if not QUIET:
            debug_info(f"[CMT] <eject_entry> ejecting entry: ({lpa}, {repr(leaving_entry)})")
            
    
    def add_entry(self, lpa: int, address: FlashAddress, dirty: bool) -> None:
        entry = cmt_entry(address=address, dirty=dirty)
        if not QUIET:
            debug_info(f"[CMT] <add_entry> adding entry: ({lpa}, {repr(entry)})")
        if len(self.cache) >= CMT_SIZE:
            lru_lpa = self.lru_list[-1]
            self.address_mapping_unit.generate_mapping_write_transaction(self.cache, lru_lpa//LPA_NO_PER_MAPPING_PAGE)
        self.cache[lpa] = entry
        self.lru_list.insert(0, lpa)

@dataclass
class blockBKE:
    invalid_page_count: int = 0
    valid_page_count: int = 0
    free_page_count: int = PAGE_PER_BLOCK
    write_frontier: int = 0  # 下次写入的目标page_id
    wl_level: int = 0  # 记录该block被erase的次数
    valid_pages: set[int] = field(default_factory=set)
    invalid_pages: set[int] = field(default_factory=set)

@dataclass
class PlaneBKE:
    write_frontier_block: int
    free_block_pool: set[int]
    block_entries: list[blockBKE] = field(default_factory=list)
    free_page_count: int = PAGE_PER_BLOCK * BLOCK_PER_PLANE
    valid_page_count: int = 0
    invalid_page_count: int = 0
    def __init__(self) -> None:
        self.block_entries = [blockBKE() for _ in range(BLOCK_PER_PLANE)]
        self.write_frontier_block = 0
        self.free_block_pool = set[int](range(BLOCK_PER_PLANE))
        self.free_page_count = PAGE_PER_BLOCK * BLOCK_PER_PLANE
        self.valid_page_count = 0
        self.invalid_page_count = 0
        # GC：当前 plane 上正在擦除的目标 block（与 _gc 的 erase_target 一致）；GC_ERASE 完成后清除
        self.gc_erase_barrier_block_id: int | None = None
        # GC/WL：当前 maintenance 流程保护中的 block 集合（source / destination / erase target）
        self.gc_wl_barrier_blocks: set[int] = set()
        # Fixed metadata/mapping-page blocks.  User writes and maintenance must
        # never allocate or erase these blocks.
        self.metadata_blocks: set[int] = set()



class Block_Manager:
    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating Block Manager construction...")
        assert self.block_keeping_book is not None, "Block Manager block_keeping_book is not set"
        self._construction_valid = True
        if not QUIET:
            print("Block Manager construction validation complete.")

    def __init__(self,
                 channel_no=CHANNEL_NO,
                 chip_no_per_channel=CHIP_PER_CHANNEL,
                 die_no_per_chip=DIE_PER_CHIP,
                 plane_no_per_die=PLANE_PER_DIE,
                 block_no_per_plane=BLOCK_PER_PLANE,
                 pages_per_block=PAGE_PER_BLOCK):
        if not QUIET:
            print("Initializing Block Manager...")
        self.channel_no = channel_no
        self.chip_no_per_channel = chip_no_per_channel
        self.die_no_per_chip = die_no_per_chip
        self.plane_no_per_die = plane_no_per_die
        self.block_no_per_plane = block_no_per_plane
        self.pages_per_block = pages_per_block
        self._construction_valid = False
        self.lpa_protected_book = dict[int, Transaction]()
        self.lpa_barrier_waiters: dict[int, list[Transaction]] = {}
        self.user_program_block_counts: dict[tuple[int, int, int, int, int], int] = {}
        self.mvpn_protected_book = dict[int, Transaction]()
        self.gc_wl_unit: GC_WL_Unit
        self.cache_manager = None
        # Write backpressure: per-physical-plane waiting queues.
        self.waiting_writes: dict[PlaneKey, list[Transaction]] = {}
        self.waiting_write_lpa_counts: dict[int, int] = {}
        self.last_precondition_stats: dict[str, Any] = {}
        self.STOP_SERVICING_WRITES_THRESHOLD = 1   # reserve ≥1 block for overwrites/GC
        self.GC_RESERVE_BLOCKS = 1
        if not QUIET:
            print("Initializing block keeping book...")
        # 结构为：channel -> chip -> die -> plane -> [blockBKE, ...]，与 address 前 5 维一致
        self.block_keeping_book = [
            [
                [
                    [
                        PlaneBKE() for _ in range(plane_no_per_die)
                    ]
                    for _ in range(die_no_per_chip)
                ]
                for _ in range(chip_no_per_channel)
            ]
            for _ in range(channel_no)
        ]
        if not QUIET:
            print("Block Manager initialization complete.")

    def apply_runtime_config(self, runtime: RuntimeConfig) -> None:
        self.STOP_SERVICING_WRITES_THRESHOLD = runtime.stop_servicing_writes_threshold
        self.GC_RESERVE_BLOCKS = runtime.gc_reserve_blocks

    @staticmethod
    def _precondition_item_lpa(item: Any) -> int:
        if isinstance(item, dict):
            return int(item["lpa"])
        return int(item)

    @staticmethod
    def _precondition_item_valid_bitmap(item: Any, default_valid_bitmap: Any = None) -> list[int]:
        if isinstance(item, dict) and "valid_bitmap" in item:
            raw_valid_bitmap = item.get("valid_bitmap")
        elif default_valid_bitmap is not None:
            raw_valid_bitmap = default_valid_bitmap
        else:
            raw_valid_bitmap = [1] * SECTOR_PER_PAGE
        valid_bitmap = [0] * SECTOR_PER_PAGE
        for i in range(min(SECTOR_PER_PAGE, len(raw_valid_bitmap))):
            valid_bitmap[i] = 1 if raw_valid_bitmap[i] else 0
        return valid_bitmap

    @staticmethod
    def _precondition_item_payload(
        item: Any,
        valid_bitmap: list[int],
        default_data_value: int = 0xAA,
    ) -> list[int]:
        if isinstance(item, dict) and "data" in item:
            raw_data = item.get("data")
        elif isinstance(item, dict) and "data_value" in item:
            raw_data = item.get("data_value")
        else:
            raw_data = default_data_value
        if isinstance(raw_data, list):
            payload_source = raw_data
        else:
            payload_source = [int(raw_data)] * SECTOR_PER_PAGE
        payload = [INVALID_DATA] * SECTOR_PER_PAGE
        for i in range(min(SECTOR_PER_PAGE, len(payload_source))):
            if valid_bitmap[i] == 1:
                payload[i] = payload_source[i]
        return payload

    @staticmethod
    def _plane_key(addr: FlashAddress) -> PlaneKey:
        return (addr.channel, addr.chip, addr.die, addr.plane)

    def _wear_skew_for_plane(self, plane_bke: PlaneBKE) -> int:
        levels = [entry.wl_level for entry in plane_bke.block_entries]
        if not levels:
            return 0
        return max(levels) - min(levels)

    def _record_plane_snapshot(self, plane_addr: FlashAddress) -> None:
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is None:
            return
        plane_bke = self.get_plane_bke(plane_addr)
        recorder.note_plane_pool_snapshot(
            plane_addr,
            free_pool_count=len(plane_bke.free_block_pool),
            wear_skew=self._wear_skew_for_plane(plane_bke),
            waiting_write_count=len(self.waiting_writes.get(self._plane_key(plane_addr), [])),
        )

    def get_write_frontier(self, plane_address: FlashAddress) -> FlashAddress:
        channel_id = plane_address.channel
        chip_id = plane_address.chip
        die_id = plane_address.die
        plane_id = plane_address.plane
        plane_bke = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
        block_id = plane_bke.write_frontier_block
        bke = plane_bke.block_entries[block_id]
        if block_id in plane_bke.metadata_blocks or bke.write_frontier >= PAGE_PER_BLOCK:
            block_id = self.select_wl_aware_free_block(plane_address)
            if block_id < 0:
                debug_info(
                    f"[Block Manager] <get_write_frontier> WARNING: plane {plane_id} "
                    "has no eligible free blocks; caller should enforce backpressure"
                )
                return None
            plane_bke.write_frontier_block = block_id
            bke = plane_bke.block_entries[block_id]
        page_id = bke.write_frontier
        debug_info(f"[Block Manager] <get_write_frontier> plane_address: {plane_address}, block {block_id}, page: {page_id}")
        if page_id == 0:
            plane_bke.free_block_pool.discard(block_id)
        bke.write_frontier += 1
        if bke.write_frontier > PAGE_PER_BLOCK:
            raise ValueError(
                f"[Block Manager] <get_write_frontier> plane {plane_id} block {block_id} overflow"
            )
        # write frontier 前移时立即更新 free page count；free/valid page count 在写事务完成时更新
        address = FlashAddress(channel=channel_id, chip=chip_id, die=die_id, plane=plane_id, sub_plane=block_id, page=page_id)
        debug_info(f"[Block Manager] plane {plane_address} free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")
        return address

    def allocate_gc_write_page(self, plane_address: FlashAddress, block_id: int | None = None) -> FlashAddress:
        """Allocate a page within a specific block (for GC migration target).

        If no free block is available, proactively triggers GC and retries.
        """
        channel_id = plane_address.channel
        chip_id = plane_address.chip
        die_id = plane_address.die
        plane_id = plane_address.plane
        plane_bke = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
        if block_id is None:
            block_id = self.select_wl_aware_free_block(plane_address)
        if block_id < 0:
            # No eligible free block — trigger GC and try once more
            self.gc_wl_unit.check_gc()
            block_id = self.select_wl_aware_free_block(plane_address)
        if block_id < 0:
            raise ValueError(
                "[Block_Manager] allocate_gc_write_page: no eligible destination block "
                "even after triggering GC — plane may be completely exhausted"
            )
        if block_id in plane_bke.metadata_blocks:
            raise ValueError("[Block_Manager] allocate_gc_write_page: metadata block selected")
        bke = plane_bke.block_entries[block_id]
        if bke.write_frontier >= PAGE_PER_BLOCK:
            raise ValueError(f"[Block_Manager] allocate_gc_write_page: block {block_id} is full")
        page_id = bke.write_frontier
        bke.write_frontier += 1
        bke.free_page_count -= 1
        plane_bke.free_page_count -= 1
        if page_id == 0:
            plane_bke.free_block_pool.discard(block_id)
        if bke.write_frontier == PAGE_PER_BLOCK:
            if block_id in plane_bke.free_block_pool:
                plane_bke.free_block_pool.discard(block_id)
        return FlashAddress(
            channel=channel_id,
            chip=chip_id,
            die=die_id,
            plane=plane_id,
            sub_plane=block_id,
            page=page_id,
        )

    def select_wl_aware_free_block(
        self,
        plane_address: FlashAddress,
        *,
        prefer_highest_wl: bool = False,
        exclude_blocks: set[int] | None = None,
    ) -> int:
        if getattr(self, "gc_wl_unit", None) is not None:
            return self.gc_wl_unit.select_wl_aware_free_block(
                plane_address,
                prefer_highest_wl=prefer_highest_wl,
                exclude_blocks=exclude_blocks,
            )

        plane_bke = self.get_plane_bke(plane_address)
        exclude_blocks = exclude_blocks or set()
        exclude_blocks = exclude_blocks | plane_bke.metadata_blocks
        candidates = sorted(plane_bke.free_block_pool - exclude_blocks)
        return candidates[0] if candidates else -1
    
    def _set_barrier(self, tr: Transaction):
        if not QUIET:
            debug_info(f"[Block Manager] <_set_barrier> setting barrier for tr: {repr(tr)}")
        if tr.type in [
            TransactionType.USER_WRITE,
            TransactionType.USER_STATIC_WRITE,
            TransactionType.GC_WRITE,
        ]:
            owner = self.lpa_protected_book.get(tr.lpa)
            if owner is None:
                self.lpa_protected_book[tr.lpa] = tr
            elif owner is not tr:
                waiters = self.lpa_barrier_waiters.setdefault(tr.lpa, [])
                if not any(waiter is tr for waiter in waiters):
                    waiters.append(tr)
            if tr.type in (TransactionType.USER_WRITE, TransactionType.USER_STATIC_WRITE):
                self._track_user_program_block(tr)
        elif tr.type == TransactionType.MAPPING_WRITE:
            if tr.mvpn not in self.mvpn_protected_book:
                self.mvpn_protected_book[tr.mvpn] = tr
        else:
            raise ValueError(f"[Block Manager] <_set_barrier> unknown transaction type: {tr.type}")

    @staticmethod
    def _program_block_key(tr: Transaction) -> tuple[int, int, int, int, int] | None:
        addr = tr.address
        if addr is None or addr.sub_plane < 0:
            return None
        return (addr.channel, addr.chip, addr.die, addr.plane, addr.sub_plane)

    def _track_user_program_block(self, tr: Transaction) -> None:
        if getattr(tr, "_user_program_block_tracked", False):
            return
        key = self._program_block_key(tr)
        if key is None:
            return
        self.user_program_block_counts[key] = self.user_program_block_counts.get(key, 0) + 1
        tr._user_program_block_tracked = True
        tr._user_program_block_key = key

    def _untrack_user_program_block(self, tr: Transaction) -> None:
        if not getattr(tr, "_user_program_block_tracked", False):
            return
        key = getattr(tr, "_user_program_block_key", None)
        if key is not None:
            count = self.user_program_block_counts.get(key, 0)
            if count <= 1:
                self.user_program_block_counts.pop(key, None)
            else:
                self.user_program_block_counts[key] = count - 1
        tr._user_program_block_tracked = False
        tr._user_program_block_key = None

    def _release_lpa_barrier(self, tr: Transaction) -> None:
        """Release only *tr*'s barrier and promote the next same-LPA write."""
        if self.lpa_protected_book.get(tr.lpa) is not tr:
            waiters = self.lpa_barrier_waiters.get(tr.lpa)
            if waiters and tr in waiters:
                waiters.remove(tr)
                if not waiters:
                    self.lpa_barrier_waiters.pop(tr.lpa, None)
                self._untrack_user_program_block(tr)
            return
        self._untrack_user_program_block(tr)
        waiters = self.lpa_barrier_waiters.get(tr.lpa)
        if waiters:
            self.lpa_protected_book[tr.lpa] = waiters.pop(0)
            if not waiters:
                self.lpa_barrier_waiters.pop(tr.lpa, None)
            return
        self.lpa_protected_book.pop(tr.lpa, None)

    def iter_lpa_barrier_transactions(self):
        yield from self.lpa_protected_book.values()
        for waiters in self.lpa_barrier_waiters.values():
            yield from waiters

    def has_pending_host_write(self, lpa: int) -> bool:
        if self.waiting_write_lpa_counts.get(lpa, 0) > 0:
            return True
        owner = self.lpa_protected_book.get(lpa)
        if owner is not None and owner.type in (
            TransactionType.USER_WRITE,
            TransactionType.USER_STATIC_WRITE,
        ):
            return True
        for waiter in self.lpa_barrier_waiters.get(lpa, []):
            if waiter.type in (
                TransactionType.USER_WRITE,
                TransactionType.USER_STATIC_WRITE,
            ):
                return True
        return False

    def _on_transaction_serviced(self, tr: Transaction) -> None:
        """
        Handle transaction serviced event.
        1. remove barrier from protected book
        2. check gc
        3. update block keeping book
        """
        if tr.type in [TransactionType.USER_WRITE]:
            self._release_lpa_barrier(tr)
            self._mark_valid(tr.address)
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_physical_write(tr)
            if tr.invalidate_target is not None:
                self._mark_invalid(tr.invalidate_target)
            self.gc_wl_unit.address_mapping_unit.on_host_write_complete(tr)
            self.gc_wl_unit.check_gc_for_plane(tr.address)
        elif tr.type == TransactionType.USER_STATIC_WRITE:
            self._release_lpa_barrier(tr)
        elif tr.type == TransactionType.GC_WRITE:
            self._release_lpa_barrier(tr)
            recorder = REQUEST_LATENCY_RECORDER()
            if recorder is not None:
                recorder.note_physical_write(tr)
            self.gc_wl_unit.address_mapping_unit.apply_gc_write_complete(tr)
        elif tr.type == TransactionType.MAPPING_WRITE:
            if self.mvpn_protected_book.get(tr.mvpn) is tr:
                self.mvpn_protected_book.pop(tr.mvpn, None)
        elif tr.type == TransactionType.GC_ERASE:
            self.lpa_protected_book.pop(tr.lpa) if tr.lpa in self.lpa_protected_book else None
            self.finalize_gc_erase(
                tr.address,
                reason=tr.maintenance_reason or "gc",
            )

    def get_block_bke(self, addr: FlashAddress) -> blockBKE:
        channel_id, chip_id, die_id, plane_id, block_id = addr.channel, addr.chip, addr.die, addr.plane, addr.sub_plane
        return self.block_keeping_book[channel_id][chip_id][die_id][plane_id].block_entries[block_id]

    def get_plane_bke(self, addr: FlashAddress) -> PlaneBKE:
        channel_id, chip_id, die_id, plane_id = addr.channel, addr.chip, addr.die, addr.plane
        return self.block_keeping_book[channel_id][chip_id][die_id][plane_id]

    def is_free(self, addr: FlashAddress) -> bool:
        bke = self.get_block_bke(addr)
        return len(bke.valid_pages) == 0 and len(bke.invalid_pages) == 0 and len(bke.free_pages) != 0

    def is_protected(self, addr: FlashAddress) -> bool:
        # 这里按需求增加是否保护的判断，可添加bke的protected属性
        bke = self.block_keeping_book[addr.channel][addr.chip][addr.die][addr.plane][addr.sub_plane]
        return bke.page_protected[addr.page]
    
    def _mark_valid(self, addr: FlashAddress, *, reserved: bool = False):
        debug_info(f"[Block Manager] <_mark_valid> {addr}")
        bke = self.get_block_bke(addr)
        plane_bke = self.get_plane_bke(addr)
        if not reserved:
            bke.free_page_count -= 1
            plane_bke.free_page_count -= 1
        if bke.free_page_count == 0:
            plane_bke.free_block_pool.discard(addr.sub_plane)
        bke.valid_pages.add(addr.page)          # update valid_pages when write transaction completed
        bke.valid_page_count += 1
        plane_bke.valid_page_count += 1
        debug_info(f"[Block Manager] <_mark_valid> plane_bke updated! {addr}, free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")

    def _mark_invalid(self, addr: FlashAddress):
        debug_info(f"[Block Manager] <_mark_invalid> {addr}")
        bke = self.get_block_bke(addr)
        if addr.page not in bke.valid_pages:
            # 页面可能已被之前的 GC/映射写入操作标记为无效，幂等跳过
            debug_info(f"[Block Manager] <_mark_invalid> address {addr} already invalid, skipping")
            return
        plane_bke = self.get_plane_bke(addr)
        bke.invalid_pages.add(addr.page)          # update invalid_pages when an update write transaction is issued
        bke.invalid_page_count += 1
        plane_bke.invalid_page_count += 1
        bke.valid_pages.remove(addr.page)
        bke.valid_page_count -= 1
        plane_bke.valid_page_count -= 1

    def finalize_gc_erase(self, addr: FlashAddress, *, reason: str = "gc") -> None:
        """After GC_ERASE: reset BKE/plane counts, return block to free pool, clear PHY storage."""
        bke = self.get_block_bke(addr)
        plane_bke = self.get_plane_bke(addr)
        old_free_page_count = bke.free_page_count
        plane_bke.valid_page_count -= bke.valid_page_count
        plane_bke.invalid_page_count -= bke.invalid_page_count
        bke.valid_pages.clear()
        bke.invalid_pages.clear()
        bke.valid_page_count = 0
        bke.invalid_page_count = 0
        bke.free_page_count = PAGE_PER_BLOCK
        bke.write_frontier = 0
        bke.wl_level += 1
        plane_bke.free_page_count += PAGE_PER_BLOCK - old_free_page_count
        if addr.sub_plane not in plane_bke.metadata_blocks:
            plane_bke.free_block_pool.add(addr.sub_plane)
        plane_bke.gc_erase_barrier_block_id = None
        plane_bke.gc_wl_barrier_blocks.clear()
        phy = self.gc_wl_unit.tsu.phy
        if phy is not None:
            phy.clear_block_pages(addr)
        debug_info(f"[Block Manager] <finalize_gc_erase> plane {addr.plane} block {addr.sub_plane} erased, free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_gc_erase_completed(addr, bke.wl_level)
        self._record_plane_snapshot(FlashAddress(
            channel=addr.channel,
            chip=addr.chip,
            die=addr.die,
            plane=addr.plane,
            sub_plane=-1,
            page=-1,
        ))
        # Wake up writes blocked by backpressure on this plane
        retried = self._retry_waiting_writes(addr)
        plane_addr = FlashAddress(
            channel=addr.channel,
            chip=addr.chip,
            die=addr.die,
            plane=addr.plane,
            sub_plane=-1,
            page=-1,
        )
        plane_key = self._plane_key(plane_addr)
        if retried == 0 and self.waiting_writes.get(plane_key):
            self._record_plane_snapshot(plane_addr)
            self.gc_wl_unit.check_gc_for_plane(plane_addr)
        self.gc_wl_unit.on_erase_complete(addr, reason=reason)

    # ── Write backpressure helpers ─────────────────────────────────────────

    def get_free_pool_count(self, plane_addr: FlashAddress) -> int:
        """Return the number of free blocks in *plane_addr*'s pool."""
        return len(self.get_plane_bke(plane_addr).free_block_pool)

    def should_backpressure_first_write(self, plane_addr: FlashAddress) -> bool:
        plane_bke = self.get_plane_bke(plane_addr)
        pool_size = len(plane_bke.free_block_pool)
        reserve_limit = self.STOP_SERVICING_WRITES_THRESHOLD + self.GC_RESERVE_BLOCKS
        if pool_size > reserve_limit:
            return False
        if pool_size <= 0:
            return True

        gc_can_progress = False
        if getattr(self, "gc_wl_unit", None) is not None:
            gc_can_progress = self.gc_wl_unit.can_trigger_gc(plane_addr)
        if gc_can_progress:
            return True
        return False

    def should_backpressure_overwrite(self, plane_addr: FlashAddress) -> bool:
        plane_bke = self.get_plane_bke(plane_addr)
        if len(plane_bke.free_block_pool) > 0:
            return False
        if getattr(self, "gc_wl_unit", None) is None:
            return False
        if self.gc_wl_unit.can_trigger_gc(plane_addr):
            return True
        frontier_block = plane_bke.write_frontier_block
        if 0 <= frontier_block < self.block_no_per_plane:
            frontier_bke = plane_bke.block_entries[frontier_block]
            if self.pages_per_block - frontier_bke.write_frontier <= 1:
                return True
        return False

    def enqueue_waiting_write(self, plane_addr: FlashAddress, tr: Transaction) -> None:
        """Put *tr* into the per-plane waiting queue (blocked by backpressure)."""
        plane_key = self._plane_key(plane_addr)
        self.waiting_writes.setdefault(plane_key, []).append(tr)
        self.waiting_write_lpa_counts[tr.lpa] = self.waiting_write_lpa_counts.get(tr.lpa, 0) + 1
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_backpressure_enqueue(tr, plane_key, CURRENT_TIME())
        if tr.address.channel >= 0:
            self._record_plane_snapshot(tr.address)
        if getattr(self, "gc_wl_unit", None) is not None:
            self.gc_wl_unit.check_gc_for_plane(plane_addr)

    def _retry_waiting_writes(self, plane_addr: FlashAddress) -> int:
        """Retry the FIFO prefix for the physical plane that regained capacity.

        Mapping state is re-resolved at retry time. Once the queue head cannot be
        submitted, its entire suffix remains queued so later overwrites cannot
        overtake it.
        """
        plane_key = self._plane_key(plane_addr)
        pending = self.waiting_writes.get(plane_key)
        if not pending:
            return 0
        amu = self.gc_wl_unit.address_mapping_unit
        tsu = self.gc_wl_unit.tsu
        submitted_count = 0
        any_submitted = False
        for tr in pending:
            # *tr.address* was set to the plane address by get_plane_address_for_lpa
            # before enqueuing; reuse it as the plane_addr for allocation.
            tr_plane_addr = tr.address
            if amu is not None:
                sq_id = getattr(tr, "_mapping_sq_id", None)
                if sq_id is None and tr.source_req is not None:
                    sq_id = tr.source_req.sq_id
                if sq_id is None:
                    sq_id = 0
                resolution, old_address = amu._resolve_write_mapping_state(sq_id, tr.lpa)
            else:
                resolution, old_address = "unmapped", None
            is_overwrite = resolution != "unmapped"
            if is_overwrite:
                if self.should_backpressure_overwrite(tr_plane_addr):
                    break
            else:
                if self.should_backpressure_first_write(tr_plane_addr):
                    break
            ppa = self.get_write_frontier(tr_plane_addr)
            if ppa is None:
                break
            count = self.waiting_write_lpa_counts.get(tr.lpa, 0)
            if count <= 1:
                self.waiting_write_lpa_counts.pop(tr.lpa, None)
            else:
                self.waiting_write_lpa_counts[tr.lpa] = count - 1
            tr.address = ppa
            if amu is not None:
                amu._apply_write_mapping_resolution(
                    tr.source_req,
                    tr,
                    sq_id,
                    ppa,
                    resolution,
                    old_address,
                )
            self._set_barrier(tr)
            if tsu is not None:
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    recorder.note_backpressure_retry(tr, plane_key, CURRENT_TIME(), submitted=True)
                tsu.Submit_trans(tr)
                if self.cache_manager is not None:
                    self.cache_manager.on_waiting_flush_submitted(tr)
                any_submitted = True
            submitted_count += 1
        if submitted_count:
            del pending[:submitted_count]
        if not pending:
            self.waiting_writes.pop(plane_key, None)
        # Trigger TSU dispatch for any newly submitted transactions
        self._record_plane_snapshot(plane_addr)
        if any_submitted and tsu is not None:
            tsu.Schedule()
        return submitted_count

    def preconditioning(self, data_path: str = None, phy=None, amu=None) -> None:
        """
        严格区分 user page 和 mapping page，分两阶段：
        1. user page 赋值（plane分组，排除mapping区域）
        2. user page 映射写入mapping page（直接写PHY._storage，PageData.function=MAPPING，mvpn）
        3. 随机选取部分user lpa-ppa预热CMT
        4. 所有mvpn-ppa映射写入gtd
        """
        import json
        import os
        import random
        from collections import defaultdict

        input_stats: dict[str, Any] = {}
        default_valid_bitmap = None
        default_data_value = 0xAA
        if data_path is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_path = os.path.join(base, 'pre_data', 'precondition_data.json')

        data_source_label = (
            f"in-memory plan ({len(data_path.get('lpas') or data_path.get('records') or data_path.get('data') or [])} entries)"
            if isinstance(data_path, dict)
            else str(data_path)
        )
        if not QUIET:
            print("\n" + "=" * 80)
            print("[Block Manager] Starting preconditioning phase (data-driven)...")
            print(f"[Block Manager] Loading precondition data from: {data_source_label}")
            print("=" * 80)
        if isinstance(data_path, dict):
            input_stats = dict(data_path.get("stats") or {})
            if "lpas" in data_path:
                precondition_data = list(data_path.get("lpas") or [])
            else:
                precondition_data = list(data_path.get("records") or data_path.get("data") or [])
            default_valid_bitmap = data_path.get("valid_bitmap")
            default_data_value = int(data_path.get("data_value", default_data_value))
        elif isinstance(data_path, list):
            precondition_data = data_path
        else:
            with open(data_path, 'r') as f:
                precondition_data = json.load(f)
            input_stats = {"mode": "manual", "source": str(data_path)}

        # 获取 PHY 和 AMU 引用（由调用方传入或从 gc_wl_unit 取）
        if phy is None:
            try:
                phy = self.gc_wl_unit.tsu.phy
            except AttributeError:
                phy = None
        if amu is None:
            try:
                amu = self.gc_wl_unit.address_mapping_unit
            except AttributeError:
                amu = None
        if amu is None:
            raise RuntimeError("[Block Manager] preconditioning: AMU (address_mapping_unit) is required!")

        # Keep preconditioning aligned with the event-driven runtime layout.
        geometry = make_event_runtime_geometry()
        valid_invalid_ratio = geometry.valid_invalid_ratio
        cmt_capacity = CMT_SIZE
        cmt_ratio = getattr(geometry, "preconditioning_cmt_ratio", 0.5)
        if not (0.0 < valid_invalid_ratio <= 1.0):
            valid_invalid_ratio = 0.5
        if not (0.0 < cmt_ratio <= 1.0):
            cmt_ratio = 0.5

        # Keep preconditioning reproducible so GC/metadata regressions can be
        # compared across runs with the same trace/config inputs.
        rng = random.Random(0)
        metadata_blocks_by_plane: dict[PlaneKey, set[int]] = defaultdict(set)
        for mvpn in range(amu.mapping_page_count):
            mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
            metadata_blocks_by_plane[self._plane_key(mapping_addr)].add(mapping_addr.sub_plane)

        # 1. user page赋值（plane分组，排除mapping区域）
        user_lpa_set = set()
        user_lpa_to_ppa = dict()  # lpa -> FlashAddress
        lpa_item_map = {
            self._precondition_item_lpa(item): item for item in precondition_data
        }
        # 只分配user page区域
        for lpa in sorted(lpa_item_map):
            try:
                addr = amu.get_plane_address_for_lpa(lpa)
            except Exception:
                continue  # 跳过mapping page区域
            user_lpa_set.add(lpa)
        # plane分组
        plane_page_data = defaultdict(list)
        for lpa in sorted(user_lpa_set):
            addr = amu.get_plane_address_for_lpa(lpa)
            plane_page_data[(addr.channel, addr.chip, addr.die, addr.plane)].append(lpa)

        plane_results: list[dict[str, Any]] = []
        for channel_id in range(self.channel_no):
            for chip_id in range(self.chip_no_per_channel):
                if self._is_static_chip(chip_id):
                    debug_info(f"[Block Manager] Skipping preconditioning for static chip {chip_id}")
                    continue
                for die_id in range(self.die_no_per_chip):
                    for plane_id in range(self.plane_no_per_die):
                        lpas = plane_page_data.get((channel_id, chip_id, die_id, plane_id), [])
                        items = [lpa_item_map[lpa] for lpa in lpas]
                        # 只传入user page
                        result = self._precondition_plane_from_data(
                            channel_id, chip_id, die_id, plane_id,
                            items, valid_invalid_ratio, phy, amu, user_lpa_to_ppa, rng,
                            metadata_blocks_by_plane.get(
                                (channel_id, chip_id, die_id, plane_id),
                                set(),
                            ),
                            default_valid_bitmap=default_valid_bitmap,
                            default_data_value=default_data_value,
                        )
                        plane_results.append(result)

        # 2. user page映射写入mapping page（直接写PHY._storage，PageData.function=MAPPING，mvpn）
        # 按mvpn分组
        mvpn_to_lpas = defaultdict(list)
        for lpa, ppa in user_lpa_to_ppa.items():
            mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
            mvpn_to_lpas[mvpn].append((lpa, ppa))
        for mvpn, lpa_ppa_list in mvpn_to_lpas.items():
            mapping_addr = amu.get_plane_address_for_mvpn(mvpn)
            # 写入PHY._storage
            if phy is not None:
                pd = phy._storage[mapping_addr.channel][mapping_addr.chip][mapping_addr.die][mapping_addr.plane][mapping_addr.sub_plane][mapping_addr.page]
                pd.function = PageType.MAPPING
                pd.lpa = INVALID_LPA
                pd.mvpn = mvpn
                pd.valid_bitmap = [0] * LPA_NO_PER_MAPPING_PAGE
                pd.data = [INVALID_PPA] * LPA_NO_PER_MAPPING_PAGE
                for lpa, ppa in lpa_ppa_list:
                    idx = lpa % LPA_NO_PER_MAPPING_PAGE
                    pd.valid_bitmap[idx] = 1
                    pd.data[idx] = utils.translate_address_to_ppa(ppa)
            # 写入gtd
            amu.gtd[mvpn] = GTDEntry(address=mapping_addr)

        # 3. 随机选取部分user lpa-ppa预热CMT
        user_lpa_list = sorted(user_lpa_to_ppa.keys())
        cmt_num = int(cmt_capacity * cmt_ratio)
        for lpa in user_lpa_list[:cmt_num]:
            addr = user_lpa_to_ppa[lpa]
            amu.cmt.add_entry(lpa, addr, dirty=False)

        preconditionable_pages = int(
            input_stats.get(
                "preconditionable_pages",
                sum(int(result.get("max_valid_pages", 0)) for result in plane_results),
            )
            or 0
        )
        plane_actual_pages = [
            int(result.get("actual_pages", 0)) for result in plane_results
        ]
        summary = dict(input_stats)
        summary.setdefault("mode", "manual")
        summary.update(
            {
                "input_pages": len(precondition_data),
                "actual_pages": len(user_lpa_to_ppa),
                "actual_fill_ratio": (
                    len(user_lpa_to_ppa) / preconditionable_pages
                    if preconditionable_pages
                    else 0.0
                ),
                "preconditionable_pages": preconditionable_pages,
                "dropped_pages": sum(
                    int(result.get("dropped_pages", 0)) for result in plane_results
                ),
                "non_empty_planes": sum(1 for count in plane_actual_pages if count > 0),
                "plane_actual_min": min(plane_actual_pages) if plane_actual_pages else 0,
                "plane_actual_max": max(plane_actual_pages) if plane_actual_pages else 0,
                "cmt_warm_pages": min(cmt_num, len(user_lpa_list)),
            }
        )
        self.last_precondition_stats = summary
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.maintenance_stats["precondition"] = summary

        if not QUIET:
            print("[Block Manager] Preconditioning phase completed.")
            print(f"[Block Manager] Precondition stats: {summary}")
            print("=" * 80 + "\n")

    def _is_static_chip(self, chip_id: int) -> bool:
        """判断 chip 是否为 static chip（用于 SEARCH/COMPUTE，从末尾分配）。"""
        return chip_id >= self.chip_no_per_channel - STATIC_CHIP_PER_CHANNEL

    def _precondition_plane_from_data(
        self,
        channel_id: int, chip_id: int, die_id: int, plane_id: int,
        items: list, valid_invalid_ratio: float, phy, amu, user_lpa_to_ppa=None, rng=None,
        metadata_blocks: set[int] | None = None,
        default_valid_bitmap: Any = None,
        default_data_value: int = 0xAA,
    ) -> dict[str, Any]:
        """
        对单个 plane 进行数据驱动的预处理。

        - items: 该 plane 分到的 page data 列表（每项含 lpa, valid_bitmap, data）
        - valid_invalid_ratio: full block 中 valid page 占总页数的比例
        - phy: PHY 对象（写 _storage），可为 None（测试时跳过）
        - amu: Address_Mapping_Unit 对象（写 gmt），可为 None（测试时跳过）
        """
        plane_bke = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
        # 重置 plane 状态
        plane_bke.block_entries = [blockBKE() for _ in range(self.block_no_per_plane)]
        plane_bke.write_frontier_block = 0
        plane_bke.free_block_pool = set(range(self.block_no_per_plane))
        plane_bke.free_page_count = self.pages_per_block * self.block_no_per_plane
        plane_bke.valid_page_count = 0
        plane_bke.invalid_page_count = 0
        plane_bke.gc_erase_barrier_block_id = None
        plane_bke.gc_wl_barrier_blocks.clear()
        plane_bke.metadata_blocks = {
            bid for bid in (metadata_blocks or set())
            if 0 <= bid < self.block_no_per_plane
        }
        valid_per_full_block = max(1, int(self.pages_per_block * valid_invalid_ratio))
        pages_per_block = self.pages_per_block
        if rng is None:
            rng = random.Random(0)
        dropped = 0
        for bid in plane_bke.metadata_blocks:
            bke = plane_bke.block_entries[bid]
            bke.write_frontier = pages_per_block
            bke.free_page_count = 0
            plane_bke.free_block_pool.discard(bid)

        allocatable_blocks = [
            bid for bid in range(self.block_no_per_plane)
            if bid not in plane_bke.metadata_blocks
        ]
        if not allocatable_blocks:
            plane_bke.write_frontier_block = 0
            plane_bke.free_block_pool.clear()
            plane_bke.free_page_count = 0
            return {
                "plane": (channel_id, chip_id, die_id, plane_id),
                "input_pages": len(items),
                "actual_pages": 0,
                "dropped_pages": len(items),
                "max_valid_pages": 0,
                "assigned_blocks": 0,
            }

        num_page = len(items)
        input_pages = num_page

        # Use at most max_blocks for data; keep gc_threshold + 1 blocks reserved
        _gc_threshold = GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD
        max_blocks = max(0, len(allocatable_blocks) - _gc_threshold - 1)
        max_valid = max_blocks * pages_per_block
        if num_page > max_valid:
            dropped = num_page - max_valid
            items = items[:max_valid]
            num_page = max_valid
            if not QUIET:
                print(
                    f"[Block Manager] Precondition truncated: "
                    f"plane Ch{channel_id}Cp{chip_id}D{die_id}P{plane_id} "
                    f"dropped {dropped} entries (max {max_valid})"
                )
        if max_blocks <= 0:
            write_frontier_block_id = allocatable_blocks[0]
            plane_bke.write_frontier_block = write_frontier_block_id
            plane_bke.free_block_pool = set(allocatable_blocks[1:])
            plane_bke.free_page_count = (
                len(plane_bke.free_block_pool) * pages_per_block
                + plane_bke.block_entries[write_frontier_block_id].free_page_count
            )
            return {
                "plane": (channel_id, chip_id, die_id, plane_id),
                "input_pages": input_pages,
                "actual_pages": 0,
                "dropped_pages": input_pages,
                "max_valid_pages": max_valid,
                "assigned_blocks": 0,
            }

        if valid_invalid_ratio >= 1.0:
            all_blocks = list(allocatable_blocks)
            rng.shuffle(all_blocks)
            data_blocks = all_blocks[:max_blocks]
            full_block_count = num_page // pages_per_block
            partial_pages = num_page % pages_per_block
            full_block_ids = data_blocks[:full_block_count]
            partial_block_id = (
                data_blocks[full_block_count]
                if partial_pages > 0 and full_block_count < len(data_blocks)
                else None
            )

            data_idx = 0

            def write_preconditioned_page(block_id: int, page_idx: int) -> None:
                nonlocal data_idx
                item = items[data_idx]
                data_idx += 1
                lpa = self._precondition_item_lpa(item)
                if phy is not None:
                    pd = phy._storage[channel_id][chip_id][die_id][plane_id][block_id][page_idx]
                    valid_bitmap = self._precondition_item_valid_bitmap(
                        item,
                        default_valid_bitmap,
                    )
                    payload = self._precondition_item_payload(
                        item,
                        valid_bitmap,
                        default_data_value,
                    )
                    pd.lpa = lpa
                    pd.mvpn = INVALID_MVPN
                    pd.valid_bitmap = valid_bitmap
                    pd.data = payload
                    pd.function = PageType.USER
                if user_lpa_to_ppa is not None:
                    user_lpa_to_ppa[lpa] = FlashAddress(
                        channel=channel_id,
                        chip=chip_id,
                        die=die_id,
                        plane=plane_id,
                        sub_plane=block_id,
                        page=page_idx,
                    )

            for block_id in full_block_ids:
                bke = plane_bke.block_entries[block_id]
                bke.write_frontier = pages_per_block
                bke.free_page_count = 0
                bke.valid_pages = set(range(pages_per_block))
                bke.valid_page_count = pages_per_block
                bke.invalid_pages = set()
                bke.invalid_page_count = 0
                plane_bke.valid_page_count += pages_per_block
                for page_idx in range(pages_per_block):
                    write_preconditioned_page(block_id, page_idx)

            if partial_block_id is not None:
                bke = plane_bke.block_entries[partial_block_id]
                bke.write_frontier = partial_pages
                bke.free_page_count = pages_per_block - partial_pages
                bke.valid_pages = set(range(partial_pages))
                bke.valid_page_count = partial_pages
                bke.invalid_pages = set()
                bke.invalid_page_count = 0
                plane_bke.valid_page_count += partial_pages
                for page_idx in range(partial_pages):
                    write_preconditioned_page(partial_block_id, page_idx)
                write_frontier_block_id = partial_block_id
                used_blocks = set(full_block_ids)
                used_blocks.add(partial_block_id)
                free_blocks = {bid for bid in all_blocks if bid not in used_blocks}
            else:
                used_blocks = set(full_block_ids)
                unused_blocks = [bid for bid in all_blocks if bid not in used_blocks]
                if unused_blocks:
                    write_frontier_block_id = unused_blocks[0]
                    free_blocks = set(unused_blocks[1:])
                else:
                    write_frontier_block_id = data_blocks[-1] if data_blocks else allocatable_blocks[0]
                    free_blocks = set()
                frontier_bke = plane_bke.block_entries[write_frontier_block_id]
                if write_frontier_block_id not in full_block_ids:
                    frontier_bke.write_frontier = 0
                    frontier_bke.free_page_count = pages_per_block

            plane_bke.write_frontier_block = write_frontier_block_id
            plane_bke.free_block_pool = free_blocks
            plane_bke.free_page_count = (
                len(plane_bke.free_block_pool) * pages_per_block
                + plane_bke.block_entries[write_frontier_block_id].free_page_count
            )
            debug_info(f"[Block Manager] <preconditioning> Channel {channel_id} Chip {chip_id} Die {die_id} Plane {plane_id}:")
            debug_info(f"  - Data entries: {num_page} | Compact valid-only blocks: {len(full_block_ids)} full, partial={partial_block_id is not None}")
            debug_info(f"  - Write frontier block: {write_frontier_block_id}")
            debug_info(f"  - Free block pool: {len(plane_bke.free_block_pool)}")
            return {
                "plane": (channel_id, chip_id, die_id, plane_id),
                "input_pages": input_pages,
                "actual_pages": data_idx,
                "dropped_pages": dropped,
                "max_valid_pages": max_valid,
                "assigned_blocks": len(full_block_ids) + (1 if partial_block_id is not None else 0),
            }

        # ── Binomial-distributed valid page counts per block (MQSim-style) ──
        # Generate blocks with varying fill levels: some nearly empty (good GC
        # victims), some nearly full.  Total valid pages across all blocks = num_page.
        rho = num_page / max(max_valid, 1)
        valid_counts: list[int] = []
        for _ in range(max_blocks):
            count = sum(1 for _ in range(pages_per_block) if rng.random() < rho)
            valid_counts.append(count)
        # Scale to hit num_page exactly
        s = sum(valid_counts)
        if s > 0:
            scale = num_page / s
            valid_counts = [max(0, min(pages_per_block, round(c * scale))) for c in valid_counts]
        # Adjust remainder
        diff = num_page - sum(valid_counts)
        for _ in range(abs(diff)):
            if diff > 0:
                candidates = [i for i, c in enumerate(valid_counts) if c < pages_per_block]
                if not candidates:
                    break
                valid_counts[rng.choice(candidates)] += 1
            else:
                candidates = [i for i, c in enumerate(valid_counts) if c > 0]
                if not candidates:
                    break
                valid_counts[rng.choice(candidates)] -= 1

        # ── Assign blocks ──
        all_blocks = list(allocatable_blocks)
        rng.shuffle(all_blocks)
        assigned_blocks: list[int] = []  # blocks that get ≥ 1 valid page
        for idx, vc in enumerate(valid_counts):
            if idx >= len(all_blocks):
                break
            if vc <= 0:
                continue  # blocks with 0 valid pages stay in free pool
            bid = all_blocks[idx]
            assigned_blocks.append(bid)
            bke = plane_bke.block_entries[bid]
            bke.write_frontier = pages_per_block  # fully written
            bke.free_page_count = 0
            all_pages = list(range(pages_per_block))
            rng.shuffle(all_pages)
            valid_positions = set(all_pages[:vc])
            invalid_positions = set(all_pages[vc:])
            bke.valid_pages = valid_positions
            bke.valid_page_count = vc
            bke.invalid_pages = invalid_positions
            bke.invalid_page_count = pages_per_block - vc
            plane_bke.valid_page_count += vc
            plane_bke.invalid_page_count += pages_per_block - vc

        # Write the physical data into assigned blocks
        data_idx = 0
        for block_id in assigned_blocks:
            bke = plane_bke.block_entries[block_id]
            for page_idx in sorted(bke.valid_pages):
                if data_idx >= len(items):
                    break
                item = items[data_idx]
                data_idx += 1
                lpa = self._precondition_item_lpa(item)
                if phy is not None:
                    pd = phy._storage[channel_id][chip_id][die_id][plane_id][block_id][page_idx]
                    valid_bitmap = self._precondition_item_valid_bitmap(
                        item,
                        default_valid_bitmap,
                    )
                    payload = self._precondition_item_payload(
                        item,
                        valid_bitmap,
                        default_data_value,
                    )
                    pd.lpa = lpa
                    pd.mvpn = INVALID_MVPN
                    pd.valid_bitmap = valid_bitmap
                    pd.data = payload
                    pd.function = PageType.USER
                if user_lpa_to_ppa is not None:
                    addr = FlashAddress(
                        channel=channel_id, chip=chip_id, die=die_id, plane=plane_id,
                        sub_plane=block_id, page=page_idx,
                    )
                    user_lpa_to_ppa[lpa] = addr

        # Remaining blocks (not assigned) go to free pool
        used_set = set(assigned_blocks)
        # Pick a write_frontier_block from unused blocks
        unused = [b for b in all_blocks if b not in used_set]
        if unused:
            write_frontier_block_id = rng.choice(unused)
            unused.remove(write_frontier_block_id)
            frontier_bke = plane_bke.block_entries[write_frontier_block_id]
            frontier_bke.free_page_count = pages_per_block
            frontier_bke.write_frontier = 0
        else:
            write_frontier_block_id = 0
        plane_bke.write_frontier_block = write_frontier_block_id
        plane_bke.free_block_pool = set(unused)
        plane_bke.free_page_count = (
            len(plane_bke.free_block_pool) * pages_per_block
            + plane_bke.block_entries[write_frontier_block_id].free_page_count
        )
        debug_info(f"[Block Manager] <preconditioning> Channel {channel_id} Chip {chip_id} Die {die_id} Plane {plane_id}:")
        debug_info(f"  - Data entries: {num_page} | Assigned blocks: {len(assigned_blocks)}/{max_blocks}")
        debug_info(f"  - Write frontier block: {write_frontier_block_id}")
        debug_info(f"  - Free block pool: {len(plane_bke.free_block_pool)}")
        return {
            "plane": (channel_id, chip_id, die_id, plane_id),
            "input_pages": input_pages,
            "actual_pages": data_idx,
            "dropped_pages": dropped,
            "max_valid_pages": max_valid,
            "assigned_blocks": len(assigned_blocks),
        }

class TSU:
    """Transaction Scheduling Unit — Out-of-Order version.

    对标 MQSim TSU_OutOfOrder。管理 9 级调度队列，以 channel 为单位轮询 chip，
    按 读 > 写 > 擦除 的优先级向 PHY 下发命令。
    """

    def __init__(self):
        if not QUIET:
            print("Initializing TSU...")
        self._construction_valid: bool = False
        self._onfly_schedule_req_no = 0
        # Scheduling priority order (highest first)
        self.sched_priority = [
            TransactionType.MAPPING_READ,
            TransactionType.USER_SEARCH,
            TransactionType.USER_COMPUTE,
            TransactionType.USER_READ,
            TransactionType.MAPPING_WRITE,
            TransactionType.USER_WRITE,
            TransactionType.GC_READ,
            TransactionType.GC_WRITE,
            TransactionType.GC_ERASE,
            TransactionType.USER_STATIC_WRITE,
        ]
        self.read_priority = (
            TransactionType.MAPPING_READ,
            TransactionType.USER_READ,
            TransactionType.GC_READ,
        )
        self.write_priority = (
            TransactionType.MAPPING_WRITE,
            TransactionType.USER_WRITE,
            TransactionType.GC_WRITE,
        )
        # deques[channel][chip][type] = list of Transaction
        self.queues = [
            [{key: [] for key in self.sched_priority}
             for _ in range(CHIP_PER_CHANNEL)]
            for _ in range(CHANNEL_NO)
        ]
        self._pending_chip_transactions = [
            [0 for _ in range(CHIP_PER_CHANNEL)]
            for _ in range(CHANNEL_NO)
        ]
        self.block_manager: Block_Manager
        self.channel_no = CHANNEL_NO
        self.chip_no_per_channel = CHIP_PER_CHANNEL
        self.round_robin_turn = [0] * self.channel_no
        self.phy : PHY
        self.cache_pressure_drain_mode = False
        self._pending_cache_pressure_writes = 0
        # Register channel/chip idle callbacks so PHY can trigger re-scheduling
        # self.phy.connect_channel_idle_signal(self._on_channel_idle)
        # self.phy.connect_chip_idle_signal(self._on_chip_idle)
        self._construction_valid: bool = False
        if not QUIET:
            print("TSU initialization complete.")
    
    def _reschedule(self, tr: Transaction):
        self.Prepare_trans_submission()
        if not QUIET:
            debug_info(f"[TSU] <_reschedule> transaction serviced, rescheduling: {repr(tr)}")
        self.Schedule()
        return

    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating TSU construction...")
        assert self.block_manager is not None, "TSU block_manager is not set"
        assert self.phy is not None, "TSU PHY is not set"
        self._construction_valid = True
        self.block_manager.Validate_construction()
        self.phy.Validate_construction()
        if not QUIET:
            print("TSU construction validation complete.")

    # ── Batch submission API ──────────────────────────────────────────────────

    def Prepare_trans_submission(self):
        """Open a submission batch; must be paired with Schedule()."""
        self._onfly_schedule_req_no += 1
        debug_info(f"[TSU] <Prepare_trans_submission> {self._onfly_schedule_req_no}")

    def Submit_trans(self, trans: Transaction):
        """Endeque a transaction to the appropriate per-chip priority deque."""
        if not QUIET:
            debug_info(f"[TSU] <Submit_trans> submitting trans: {repr(trans)}")
        if (
            not trans.report_origin_request_ids
            and trans.source_req is not None
            and trans.source_req.report_req_id is not None
        ):
            trans.report_origin_request_ids = [trans.source_req.report_req_id]
        channel = trans.address.channel
        chip    = trans.address.chip
        self.queues[channel][chip][trans.type].append(trans)
        self._pending_chip_transactions[channel][chip] += 1
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_tsu_enqueued(trans, CURRENT_TIME())

    def _has_pending_chip_work(self, chip_id) -> bool:
        return any(self.queues[chip_id[0]][chip_id[1]][key] for key in self.sched_priority)

    def _mark_dispatched(self, chip_id, count: int) -> None:
        if count <= 0:
            return
        pending = self._pending_chip_transactions[chip_id[0]][chip_id[1]] - count
        self._pending_chip_transactions[chip_id[0]][chip_id[1]] = max(0, pending)

    def start_cache_pressure_drain(self, write_count: int):
        if write_count <= 0:
            return
        self.cache_pressure_drain_mode = True
        self._pending_cache_pressure_writes += write_count
        debug_info(
            f"[TSU] <start_cache_pressure_drain> pending={self._pending_cache_pressure_writes}"
        )

    def finish_cache_pressure_write(self):
        if self._pending_cache_pressure_writes > 0:
            self._pending_cache_pressure_writes -= 1
        if self._pending_cache_pressure_writes == 0:
            self.cache_pressure_drain_mode = False
        debug_info(
            f"[TSU] <finish_cache_pressure_write> pending={self._pending_cache_pressure_writes}, "
            f"drain_mode={self.cache_pressure_drain_mode}"
        )

    def _transaction_blocked_by_barrier(self, tr: Transaction) -> bool:
        """Barrier 在下发 PHY 前检查：LPA/MVPN 与 GC erase_target 块上的 program 写。"""
        bm = self.block_manager
        book_lpa = bm.lpa_protected_book.get(tr.lpa)
        if book_lpa is not None and book_lpa is not tr:
            # GC 迁移：源块 GC_READ 与目的块 GC_WRITE 同 LPA；book 由 GC_WRITE 占位时仍须先下发 GC_READ
            if tr.type == TransactionType.GC_READ and book_lpa.type == TransactionType.GC_WRITE:
                pass
            else:
                return True
        book_mvpn = bm.mvpn_protected_book.get(tr.mvpn)
        if book_mvpn is not None and book_mvpn is not tr:
            # A mapping write may depend on an older mapping write or on a
            # read-modify-write mapping read.  Those direct dependencies must pass
            # through the MVPN barrier held by the waiting write, otherwise the
            # chain deadlocks in the TSU queue.
            if (
                book_mvpn.type == TransactionType.MAPPING_WRITE
                and tr.type == TransactionType.MAPPING_WRITE
                and tr.mvpn == book_mvpn.mvpn
            ):
                pass
            elif (
                book_mvpn.type == TransactionType.MAPPING_WRITE
                and (
                    any(dep is tr for dep in book_mvpn.rely_on_transactions)
                    or any(required is book_mvpn for required in tr.required_by_transactions)
                )
            ):
                pass
            else:
                return True
        if tr.type in (
            TransactionType.USER_WRITE,
            TransactionType.MAPPING_WRITE,
            TransactionType.USER_STATIC_WRITE,
        ):
            addr = tr.address
            if addr.sub_plane >= 0:
                plane_bke = bm.get_plane_bke(addr)
                bid = plane_bke.gc_erase_barrier_block_id
                if bid is not None and addr.sub_plane == bid:
                    return True
                if addr.sub_plane in plane_bke.gc_wl_barrier_blocks:
                    return True
        return False

    def Schedule(self):
        """Close batch and, if all batches are closed, drive scheduling.
        对标 TSU_OutOfOrder::Schedule()。
        """
        self._onfly_schedule_req_no -= 1
        debug_info(f"[TSU] <Schedule> {self._onfly_schedule_req_no}")
        if self._onfly_schedule_req_no < 0:
            self._onfly_schedule_req_no = 0  # reset (drain can call Schedule extra times)
        if self._onfly_schedule_req_no > 0:
            return
        for ch in range(self.channel_no):
            if self.channel_is_busy(ch):
                debug_info(f"[TSU] <Schedule> channel {ch} is busy, move to next channel")
                continue   # channel occupied; move to next channel
            for _ in range(self.chip_no_per_channel):
                chip_id = (ch, self.round_robin_turn[ch])
                self.try_activate(chip_id)
                self.round_robin_turn[ch] = (
                    (self.round_robin_turn[ch] + 1) % self.chip_no_per_channel
                )
                if self.channel_is_busy(ch):
                    break   # channel occupied; move to next channel

    # ── Idle callbacks from PHY ───────────────────────────────────────────────

    def _on_channel_idle(self, channel_id: int):
        """对标 handle_channel_idle_signal(): 轮询该 channel 下的 chip 尝试激活。"""
        debug_info(f"[TSU] <_on_channel_idle> handling channel {channel_id} idle")
        for _ in range(self.chip_no_per_channel):
            chip_id = (channel_id, self.round_robin_turn[channel_id])
            self.try_activate(chip_id)
            self.round_robin_turn[channel_id] = (
                (self.round_robin_turn[channel_id] + 1) % self.chip_no_per_channel
            )
            if self.phy._active_transfers[channel_id] is not None:
                debug_info(f"[TSU] <_on_channel_idle> channel {channel_id} is busy, moving to next chip")
                break
        self.phy.schedule_next_channel_transfer(channel_id)

    def _on_chip_idle(self, chip_id):
        """对标 handle_chip_idle_signal(): chip 空闲且 channel 空闲时尝试激活。"""
        channel_id = chip_id[0]
        debug_info(f"[TSU] <_on_chip_idle> handling chip {chip_id} idle")
        if not self.channel_is_busy(channel_id):
            self.try_activate(chip_id)

    # ── Top-level dispatcher ──────────────────────────────────────────────────

    def try_activate(self, chip_id) -> bool:
        """对标 process_chip_requests(): 按 读>写>擦除 顺序尝试激活 chip。"""
        if not self._has_pending_chip_work(chip_id):
            return False
        chip_bke = self.phy.get_chip_bke(chip_id)
        if (
            chip_bke.status != ChipStatus.IDLE
            and chip_bke.No_of_active_dies <= 0
            and not chip_bke.HasSuspendedCommands
            and not chip_bke._has_data_waiting
        ):
            chip_bke.No_of_active_dies = 0
            chip_bke.status = ChipStatus.IDLE
        is_static = self.is_static_chip(chip_id)
        # #region agent log
        dispatched = False
        # #endregion
        if is_static:
            # SEARCH/COMPUTE-dedicated chip: handled separately
            debug_info(f"[TSU] <try_activate> SEARCH/COMPUTE-dedicated chip {chip_id}")
            if self.try_compute(chip_id):
                debug_info(f"[TSU] <try_activate> compute dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> compute failed for chip {chip_id}")
            if not dispatched and self.try_search(chip_id):
                debug_info(f"[TSU] <try_activate> search dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> search failed for chip {chip_id}")
            if not dispatched and self.try_static_write(chip_id):
                debug_info(f"[TSU] <try_activate> static write dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> static write failed for chip {chip_id}")
            return dispatched
        if self.cache_pressure_drain_mode:
            if not dispatched and self.try_write(chip_id):
                debug_info(f"[TSU] <try_activate> drain-mode write dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> drain-mode write failed for chip {chip_id}")
            if not dispatched and self.try_read(chip_id):
                debug_info(f"[TSU] <try_activate> drain-mode read dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> drain-mode read failed for chip {chip_id}")
        else:
            if not dispatched and self.try_read(chip_id):
                debug_info(f"[TSU] <try_activate> read dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> read failed for chip {chip_id}")
            if not dispatched and self.try_write(chip_id):
                debug_info(f"[TSU] <try_activate> write dispatched for chip {chip_id}")
                dispatched = True
            else:
                debug_info(f"[TSU] <try_activate> write failed for chip {chip_id}")
        if not dispatched and self.try_erase(chip_id):
            debug_info(f"[TSU] <try_activate> erase dispatched for chip {chip_id}")
            dispatched = True
        else:
            debug_info(f"[TSU] <try_activate> erase failed for chip {chip_id}")
        return dispatched

    # ── Per-type scheduling methods ───────────────────────────────────────────

    def try_read(self, chip_id) -> bool:
        """Try to issue a read command to the chip.

        对标 TSU_OutOfOrder::service_read_transaction()。
        Checks chip status and, if necessary, suspends an ongoing WRITE/ERASE.
        Picks the two highest-priority non-empty read deques as q1 / q2.
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        chip_status = chip_bke.status
        suspension_required = False

        if chip_status != ChipStatus.IDLE:
            if chip_status == ChipStatus.READ:
                # Suspending a read for another read makes no sense
                return False
            elif chip_status in (ChipStatus.WRITE, ChipStatus.GC_WRITE):
                if not chip_bke.EnableWriteSuspend or chip_bke.HasSuspendedCommands:
                    return False
                if chip_bke.Expected_Finish_Time - CURRENT_TIME() < REASONABLE_TIME_SUSPEND_WRITE_FOR_READ:
                    return False
                suspension_required = True
            elif chip_status == ChipStatus.ERASE:
                if not chip_bke.EnableEraseSuspend or chip_bke.HasSuspendedCommands:
                    return False
                if chip_bke.Expected_Finish_Time - CURRENT_TIME() < REASONABLE_TIME_SUSPEND_ERASE_FOR_READ:
                    return False
                suspension_required = True
            else:
                return False

        chip_queues = self.queues[chip_id[0]][chip_id[1]]
        q1, q2 = None, None
        for key in self.read_priority:
            if chip_queues[key]:
                if q1 is None:
                    q1 = chip_queues[key]
                else:
                    q2 = chip_queues[key]
                    break

        if q1 is None:
            return False
        return self.issue_command(chip_id, q1, q2, suspension_required)

    def try_write(self, chip_id) -> bool:
        """Try to issue a write command to the chip.

        对标 TSU_OutOfOrder::service_write_transaction()。
        Allows Erase Suspension for writes; disallows Write-on-Write suspension.
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        chip_status = chip_bke.status
        suspension_required = False
        debug_info(f"[TSU] <try_write> chip {chip_id} status: {chip_status}")

        if chip_status != ChipStatus.IDLE:
            if chip_status in (ChipStatus.WRITE, ChipStatus.GC_WRITE, ChipStatus.READ):
                return False
            elif chip_status == ChipStatus.ERASE:
                if not chip_bke.EnableEraseSuspend or chip_bke.HasSuspendedCommands:
                    return False
                if chip_bke.Expected_Finish_Time - CURRENT_TIME() < REASONABLE_TIME_SUSPEND_ERASE_FOR_WRITE:
                    return False
                suspension_required = True
            else:
                return False

        chip_queues = self.queues[chip_id[0]][chip_id[1]]
        q1, q2 = None, None
        for key in self.write_priority:
            if chip_queues[key]:
                if q1 is None:
                    q1 = chip_queues[key]
                else:
                    q2 = chip_queues[key]
                    break

        if q1 is None:
            return False
        return self.issue_command(chip_id, q1, q2, suspension_required)

    def try_erase(self, chip_id) -> bool:
        """Try to issue an erase command to the chip.

        对标 TSU_OutOfOrder::service_erase_transaction()。
        Erase can only be issued when the chip is fully IDLE.
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        if chip_bke.status != ChipStatus.IDLE:
            return False

        q = self.queues[chip_id[0]][chip_id[1]].get(TransactionType.GC_ERASE)
        if not q:
            return False
        return self.issue_command(chip_id, q, None, False)

    def try_compute(self, chip_id) -> bool:
        """Try to issue a compute command to the chip.

        Compute 只能在 chip IDLE 时触发，直接选取 user_compute 队列，
        调用 issue_compute_command 下发。
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        if chip_bke.status != ChipStatus.IDLE:
            return False

        q = self.queues[chip_id[0]][chip_id[1]].get(TransactionType.USER_COMPUTE)
        if not q:
            return False
        return self.issue_compute_command(chip_id, q)

    def try_search(self, chip_id) -> bool:
        """Try to issue a search command to the chip.

        Search 只能在 chip IDLE 时触发，直接选取 user_search 队列，
        调用 issue_search_command 下发。
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        if chip_bke.status != ChipStatus.IDLE:
            return False

        q = self.queues[chip_id[0]][chip_id[1]].get(TransactionType.USER_SEARCH)
        if not q:
            return False
        return self.issue_search_command(chip_id, q)

    def try_static_write(self, chip_id) -> bool:
        """Try to issue a static write command to the chip.

        Static write 只能在 chip IDLE 时触发，直接选取 user_static_write 队列，
        调用 issue_static_write_command 下发。
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        if chip_bke.status != ChipStatus.IDLE:
            return False

        q = self.queues[chip_id[0]][chip_id[1]].get(TransactionType.USER_STATIC_WRITE)
        if not q:
            return False
        return self.issue_static_write_command(chip_id, q)

    # ── Command dispatch to PHY ───────────────────────────────────────────────

    def issue_command(
        self,
        chip_id,
        q1: list,
        q2,
        suspension_required: bool,
    ) -> bool:
        """按 die 粒度选取满足 plane 并行条件的 transactions 并下发给 PHY。

        对标 TSU_Base::issue_command_to_chip()。
        - 以 q1 队首 transaction 的 die 为起点，依次遍历该 chip 下每个 die。
        - 对每个 die，先扫描 q1 再扫描 q2，用 plane 位图保证每个 plane 最多选一个
          transaction，同时要求同 die 内所有 transaction 的 page 相同（multiplane
          命令约束）。
        - 找到至少一个 transaction 后立即将其合并为列表发给 PHY，清空临时槽位，然后
          返回 True；当前 die 无候选则跳到下一个 die 继续尝试。
        Returns True if a command was dispatched.
        """
        if not q1:
            raise ValueError("Issued an empty command to PHY")
        if not QUIET:
            debug_info(f"[TSU] <issue_command> q1: {q1}, q2: {q2}, suspension_required: {suspension_required}")
        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q1[0].address.die
        start_page = q1[0].address.page
        dispatched = False
        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            page_id = start_page if _step == 0 else None
            plane_vector = 0
            dispatch_slots: list = []
            q1_indices: list[int] = []
            q2_indices: list[int] = []

            for idx, tr in enumerate(q1):
                if tr.rely_on_transactions:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                if tr.address.die != die_id:
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
                    continue
                tr_page = tr.address.page
                if plane_vector == 0:
                    page_id = tr_page
                elif tr_page != page_id:
                    continue
                tr.SuspendRequired = suspension_required
                plane_vector |= 1 << tr_plane
                dispatch_slots.append(tr)
                q1_indices.append(idx)
                if len(dispatch_slots) >= plane_no:
                    break

            if q2 is not None and len(dispatch_slots) < plane_no:
                for idx, tr in enumerate(q2):
                    if tr.rely_on_transactions:
                        if not QUIET:
                            debug_info(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                        continue
                    if not tr.data_ready:
                        if not QUIET:
                            debug_info(f"[TSU] <issue_command> tr data not ready, skipping {repr(tr)}")
                        continue
                    if self._transaction_blocked_by_barrier(tr):
                        if not QUIET:
                            debug_info(f"[TSU] <issue_command> tr blocked by barrier, skipping {repr(tr)}")
                        continue
                    if tr.address.die != die_id:
                        continue
                    tr_plane = tr.address.plane
                    if plane_vector & (1 << tr_plane):
                        continue
                    tr_page = tr.address.page
                    if plane_vector == 0:
                        page_id = tr_page
                    elif tr_page != page_id:
                        continue
                    tr.SuspendRequired = suspension_required
                    plane_vector |= 1 << tr_plane
                    dispatch_slots.append(tr)
                    q2_indices.append(idx)
                    if len(dispatch_slots) >= plane_no:
                        break

            if dispatch_slots:
                dispatched = True
                debug_info(f"[TSU] <issue_command> dispatching {len(dispatch_slots)} transactions to PHY")
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    for tr in dispatch_slots:
                        recorder.note_tsu_dispatched(tr, CURRENT_TIME())
                for idx in reversed(q1_indices):
                    del q1[idx]
                if q2 is not None:
                    for idx in reversed(q2_indices):
                        del q2[idx]
                self._mark_dispatched(chip_id, len(dispatch_slots))
                self.phy.send_command_to_chip(chip_id, dispatch_slots, suspension_required)
                dispatch_slots = []
        return dispatched

    def issue_search_command(self, chip_id, q: list) -> None:
        """按 die-plane 粒度选取 search transactions 并下发给 PHY。

        Search 地址最细粒度为 address[4]（sub_plane/操作单元），address[5] 恒为 0。
        约束：每个 die 的每个 plane 中最多选中一个操作单元。
        对每个 die，从队列中收集满足约束的 transactions 后立即发给 PHY，
        找到第一个有候选的 die 后返回 True。
        """
        if not q:
            raise ValueError("Issued an empty search transactions deque to PHY")

        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q[0].address.die
        dispatched = False
        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_vector = 0
            dispatch_slots: list = []
            selected_indices: list[int] = []

            for idx, tr in enumerate(q):
                if tr.rely_on_transactions:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_search_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_search_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_search_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                if tr.address.die != die_id:
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_search_command> tr plane already selected, skipping {repr(tr)}")
                    continue
                # surppose 
                tr.SuspendRequired = False
                plane_vector |= 1 << tr_plane
                dispatch_slots.append(tr)
                selected_indices.append(idx)
                if len(dispatch_slots) >= plane_no:
                    break

            if dispatch_slots:
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    for tr in dispatch_slots:
                        recorder.note_tsu_dispatched(tr, CURRENT_TIME())
                for idx in reversed(selected_indices):
                    del q[idx]
                self._mark_dispatched(chip_id, len(dispatch_slots))
                self.phy.send_command_to_chip(chip_id, dispatch_slots, False)
                dispatched = True
        return dispatched

    def issue_compute_command(self, chip_id, q: list) -> bool:
        """按 die-plane 粒度选取 compute transactions 并下发给 PHY。

        Compute 地址最细粒度为 address[4]（sub_plane/操作单元），address[5] 恒为 0。
        约束：每个 plane 中选中的操作单元数量不超过 COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL。
        对每个 die，收集满足约束的 transactions 后立即发给 PHY，
        找到第一个有候选的 die 后返回 True。
        """
        if not q:
            raise ValueError("Issued an empty compute transactions deque to PHY")

        die_no = DIE_PER_CHIP
        max_per_plane = COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL

        start_die = q[0].address.die
        dispatched = False
        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_count: dict = {}
            dispatch_slots: list = []
            selected_indices: list[int] = []

            for idx, tr in enumerate(q):
                if tr.address.die != die_id:
                    continue
                if tr.rely_on_transactions:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_compute_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_compute_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_compute_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                tr_plane = tr.address.plane
                count = plane_count.get(tr_plane, 0)
                if count >= max_per_plane:
                    continue
                tr.SuspendRequired = False
                plane_count[tr_plane] = count + 1
                dispatch_slots.append(tr)
                selected_indices.append(idx)

            if dispatch_slots:
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    for tr in dispatch_slots:
                        recorder.note_tsu_dispatched(tr, CURRENT_TIME())
                for idx in reversed(selected_indices):
                    del q[idx]
                self._mark_dispatched(chip_id, len(dispatch_slots))
                self.phy.send_command_to_chip(chip_id, dispatch_slots, False)
                dispatched = True
        return dispatched

    def issue_static_write_command(self, chip_id, q: list) -> bool:
        """按 die-plane 粒度选取 static write transactions 并下发给 PHY。

        Static write 地址最细粒度为 address[4]（sub_plane/操作单元），address[5] 恒为 0。
        约束：每个 die 的每个 plane 中最多选中一个操作单元。
        对每个 die，从队列中收集满足约束的 transactions 后立即发给 PHY，
        找到第一个有候选的 die 后返回 True。
        """
        if not q:
            raise ValueError("Issued an empty static write transactions deque to PHY")

        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q[0].address.die
        dispatched = False
        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_vector = 0
            dispatch_slots: list = []
            selected_indices: list[int] = []

            for idx, tr in enumerate(q):
                if tr.address.die != die_id:
                    continue
                if tr.rely_on_transactions:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_static_write_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    if not QUIET:
                        debug_info(f"[TSU] <issue_static_write_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_static_write_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
                    if not QUIET:
                        debug_info(f"[TSU] <issue_static_write_command> tr plane already selected, skipping {repr(tr)}")
                    continue
                tr.SuspendRequired = False
                plane_vector |= 1 << tr_plane
                dispatch_slots.append(tr)
                selected_indices.append(idx)
                if len(dispatch_slots) >= plane_no:
                    break

            if dispatch_slots:
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    for tr in dispatch_slots:
                        recorder.note_tsu_dispatched(tr, CURRENT_TIME())
                for idx in reversed(selected_indices):
                    del q[idx]
                self._mark_dispatched(chip_id, len(dispatch_slots))
                self.phy.send_command_to_chip(chip_id, dispatch_slots, False)   
                dispatched = True
        return dispatched
    # ── Helpers ───────────────────────────────────────────────────────────────

    def channel_is_busy(self, channel_id: int) -> bool:
        return self.phy.channel_is_busy(channel_id)

    def is_static_chip(self, chip_id) -> bool:
        """Returns True if chip is dedicated to SEARCH/COMPUTE. Must match get_static_address() which maps static sub_planes to chip 0..STATIC_CHIP_PER_CHANNEL-1."""
        return chip_id[1] >= CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL


class Address_Mapping_Domain:
    def __init__(self):
        self.cmt: CMT
        self.gmt: dict[int, cmt_entry] = {}
        self._construction_valid: bool = False
    
    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating Address Mapping Domain construction...")
        assert self.cmt is not None, "Address Mapping Domain cmt is not set"
        assert self.gmt is not None, "Address Mapping Domain gmt is not set"
        self._construction_valid = True
        if not QUIET:
            print("Address Mapping Domain construction validation complete.")

    def query_cmt(self, transaction: Transaction) -> str | None:
        if self.cmt.is_cached(transaction.lpa):
            entry = self.cmt.get_cached_entry(transaction.lpa)
            transaction.address = entry.address
            return "cmt_hit"
        if transaction.lpa in self.gmt:
            entry = self.gmt[transaction.lpa]
            transaction.address = entry.address
            return "gmt_hit"
        return None

class Address_Mapping_Unit:
    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating Address Mapping Unit construction...")
        assert self.domains is not None, "Address Mapping Unit domains is not set"
        assert self.waiting_for_mapping_trans is not None, "Address Mapping Unit waiting_for_mapping_trans is not set"
        assert self.tsu is not None, "Address Mapping Unit tsu is not set"
        assert self.gtd is not None, "Address Mapping Unit gtd is not set"
        assert self.block_manager is not None, "Address Mapping Unit block_manager is not set"
        self._construction_valid = True
        for domain in self.domains:
            assert domain.gmt == self.gmt, "Address Mapping Unit domains gmt is not the same as Address Mapping Unit gmt"
            domain.Validate_construction()
        if not QUIET:
            print("Address Mapping Unit construction validation complete.")

    def __init__(self):
        if not QUIET:
            print("Initializing Address Mapping Unit...")
        self._construction_valid: bool = False
        self._plane_allocation_scheme: str = "PAGE_LEVEL"
        self.flash_geometry = make_event_runtime_geometry()
        self.domains = [Address_Mapping_Domain() for _ in range(NUM_OF_QUEUES)]
        self.waiting_for_mapping_trans: dict[int, list[Transaction]] = defaultdict(list)
        self.reads_waiting_for_lpa_write: dict[int, list[Transaction]] = defaultdict(list)
        self.tsu: TSU
        self.cmt: CMT
        self.gmt: dict[int, cmt_entry] = {}
        self.gtd: dict[int, GTDEntry] = {}
        self.block_manager: Block_Manager
        self.latest_mapping_write: dict[int, Transaction] = {}
        # Random-access region excludes static chips.
        non_static_chip_no = self.flash_geometry.chip_per_channel - self.flash_geometry.static_chip_per_channel
        self.total_random_access_pages = (
            self.flash_geometry.channel_no
            * non_static_chip_no
            * self.flash_geometry.dies
            * self.flash_geometry.planes_per_die
            * self.flash_geometry.blocks_per_plane
            * self.flash_geometry.pages_per_block
        )
        self.mapping_page_count = (
            self.total_random_access_pages + LPA_NO_PER_MAPPING_PAGE - 1
        ) // LPA_NO_PER_MAPPING_PAGE
        self.random_access_data_pages = self.total_random_access_pages - self.mapping_page_count
        self.mapping_region_start_page = self.random_access_data_pages
        if self.random_access_data_pages <= 0:
            raise ValueError(
                "[AMU] Invalid mapping layout: no random-access data pages left after reserving mapping pages"
            )
        if not QUIET:
            print(
                f"[AMU] Mapping layout: total_random_access_pages={self.total_random_access_pages}, "
                f"mapping_page_count={self.mapping_page_count}, "
                f"mapping_region=[{self.mapping_region_start_page}, {self.total_random_access_pages})"
            )
        for domain in self.domains:
            domain.gtd = self.gtd
            domain.gmt = self.gmt
        if CMT_TYPE == "seperated":
            for domain in self.domains:
                domain.cmt = CMT()
            self.cmt = None
        elif CMT_TYPE == "shared":
            self.cmt = CMT()
            for domain in self.domains:
                domain.cmt = self.cmt
        else:
            raise ValueError(f"Invalid CMT type: {CMT_TYPE}")
        for domain in self.domains:
            domain.cmt.address_mapping_unit = self

    def _mark_waiting_reads_failed(self, lpas: list[int], error_message: str) -> None:
        for lpa in lpas:
            waiting_trs = self.waiting_for_mapping_trans[lpa]
            for waiting_tr in waiting_trs:
                waiting_tr.completed = True
                waiting_tr.failed = True
                waiting_tr.error_message = error_message
                waiting_tr.rely_on_transactions = [
                    dep for dep in waiting_tr.rely_on_transactions if dep.type != TransactionType.MAPPING_READ
                ]
            waiting_trs.clear()

    def _defer_read_until_lpa_write_complete(self, tr: Transaction) -> None:
        self.reads_waiting_for_lpa_write[tr.lpa].append(tr)

    def on_host_write_complete(self, tr: Transaction) -> None:
        lpa = tr.lpa
        waiting_reads = self.reads_waiting_for_lpa_write.get(lpa)
        if not waiting_reads:
            return
        if self.block_manager.has_pending_host_write(lpa):
            return
        self.tsu.Prepare_trans_submission()
        for read_tr in waiting_reads:
            if (
                read_tr.source_req is not None
                and read_tr.source_req.completion_sent
                and read_tr.source_req.status == REQUEST_STATUS_ERROR
            ):
                continue
            sq_id = read_tr.source_req.sq_id if read_tr.source_req is not None else 0
            domain = self.domains[sq_id]
            if domain.query_cmt(read_tr) is None:
                read_tr.address = tr.address
                domain.cmt.add_entry(lpa, tr.address, dirty=False)
            self.tsu.Submit_trans(read_tr)
        waiting_reads.clear()
        self.reads_waiting_for_lpa_write.pop(lpa, None)
        self.tsu.Schedule()

    def _resolve_write_mapping_state(
        self,
        sq_id: int,
        lpa: int,
    ) -> tuple[str, FlashAddress | None]:
        domain = self.domains[sq_id]
        if domain.cmt.is_cached(lpa):
            return "cmt_hit", domain.cmt.cache[lpa].address
        if lpa in self.gmt:
            return "gmt_hit", self.gmt[lpa].address

        mvpn = lpa // LPA_NO_PER_MAPPING_PAGE
        if mvpn not in self.gtd:
            return "unmapped", None

        phy = getattr(self.tsu, "phy", None)
        if phy is None:
            raise ValueError(
                "[AMU] write mapping lookup requires PHY access for fixed metadata fallback"
            )
        entry = self.gtd[mvpn]
        addr = entry.address
        page = phy._storage[addr.channel][addr.chip][addr.die][addr.plane][addr.sub_plane][addr.page]
        if not getattr(entry, "written", True) and page.function is None:
            return "unmapped", None
        if page.function is None:
            raise ValueError(
                "[AMU] write mapping lookup touched an unwritten metadata page"
            )
        if page.function != PageType.MAPPING:
            raise ValueError(
                "[AMU] write mapping lookup touched a non-mapping metadata page"
            )
        if page.mvpn != mvpn or page.lpa != INVALID_LPA:
            raise ValueError(
                "[AMU] write mapping lookup touched a corrupted metadata page"
            )

        idx = lpa % LPA_NO_PER_MAPPING_PAGE
        if idx >= len(page.valid_bitmap):
            raise ValueError(
                "[AMU] write mapping lookup touched a malformed metadata bitmap"
            )
        if page.valid_bitmap[idx] == 0:
            return "unmapped", None
        if idx >= len(page.data) or page.data[idx] == INVALID_PPA:
            raise ValueError(
                "[AMU] write mapping lookup touched an invalid ppa in metadata page"
            )
        return "metadata_hit", utils.translate_ppa_to_address(page.data[idx])

    def _apply_write_mapping_resolution(
        self,
        req: Request | None,
        tr: Transaction,
        sq_id: int,
        page_address: FlashAddress,
        resolution: str,
        old_address: FlashAddress | None,
    ) -> None:
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_mapping_resolution(
                req,
                "uncached_write" if resolution == "unmapped" else resolution,
            )

        domain = self.domains[sq_id]
        tr.invalidate_target = None
        if resolution == "unmapped":
            domain.cmt.add_entry(tr.lpa, page_address, dirty=True)
        elif resolution == "cmt_hit":
            tr.invalidate_target = domain.cmt.update_entry(tr.lpa, page_address, dirty=True)
        else:
            tr.invalidate_target = old_address
            domain.cmt.add_entry(tr.lpa, page_address, dirty=True)
    
    def _handle_mapping_response(self, tr: Transaction):
        # handle response for tr waiting mapping info
        if tr.type == TransactionType.MAPPING_READ:
            if not QUIET:
                debug_info(f"[AMU] <_handle_mapping_response> response tr: {repr(tr)}")
            self.tsu.Prepare_trans_submission()
            # get arriving lpa in the finished mapping read transaction
            arriving_lpa = []
            for i in range(len(tr.bitmap)):
                if tr.bitmap[i] == 0:
                    continue
                lpa = i + tr.mvpn * LPA_NO_PER_MAPPING_PAGE
                arriving_lpa.append(lpa)
            if tr.failed:
                self._mark_waiting_reads_failed(
                    arriving_lpa,
                    tr.error_message or "mapping read failed",
                )
                recorder = REQUEST_LATENCY_RECORDER()
                if recorder is not None:
                    for lpa in arriving_lpa:
                        for waiting_tr in self.waiting_for_mapping_trans[lpa]:
                            recorder.note_mapping_wait_end(
                                waiting_tr.source_req,
                                str(id(tr)),
                                CURRENT_TIME(),
                            )
                debug_info("[AMU] <_handle_mapping_response> failed mapping read cleaned up")
                return
            if tr.response is None:
                raise ValueError("[AMU] <_handle_mapping_response> empty mapping read response")
            if not QUIET:
                debug_info(f"[AMU] <_handle_mapping_response> arriving_lpa: {arriving_lpa}, response: {tr.response}")
            # submit the waiting transactions for the arriving lpa, and update the cmt meanwhile
            for lpa in arriving_lpa:
                idx = lpa % LPA_NO_PER_MAPPING_PAGE
                if tr.response.valid_bitmap[idx] == 0:
                    recorder = REQUEST_LATENCY_RECORDER()
                    if recorder is not None:
                        for waiting_tr in self.waiting_for_mapping_trans[lpa]:
                            recorder.note_mapping_wait_end(
                                waiting_tr.source_req,
                                str(id(tr)),
                                CURRENT_TIME(),
                            )
                    self._mark_waiting_reads_failed(
                        [lpa],
                        f"Read request accessing invalid lpa in mapping page, lpa={lpa}",
                    )
                    continue
                ppa = tr.response.data[idx]
                if ppa == INVALID_PPA:
                    recorder = REQUEST_LATENCY_RECORDER()
                    if recorder is not None:
                        for waiting_tr in self.waiting_for_mapping_trans[lpa]:
                            recorder.note_mapping_wait_end(
                                waiting_tr.source_req,
                                str(id(tr)),
                                CURRENT_TIME(),
                            )
                    self._mark_waiting_reads_failed(
                        [lpa],
                        f"Read request accessing invalid ppa in mapping page, lpa={lpa}",
                    )
                    continue
                address = utils.translate_ppa_to_address(ppa)
                # add entry in cmt for host read path only
                if tr.source_req is not None and tr.source_req.sq_id is not None:
                    domain = self.domains[tr.source_req.sq_id]
                    if not domain.cmt.is_cached(lpa):
                        domain.cmt.add_entry(lpa, address, dirty=False)
                waiting_trs = self.waiting_for_mapping_trans[lpa]
                debug_info(f"[AMU] <_handle_mapping_response> number of waiting trs: {len(waiting_trs)}")
                for waiting_tr in waiting_trs:
                    recorder = REQUEST_LATENCY_RECORDER()
                    if recorder is not None:
                        recorder.note_mapping_wait_end(
                            waiting_tr.source_req,
                            str(id(tr)),
                            CURRENT_TIME(),
                        )
                    if (
                        waiting_tr.source_req is not None
                        and waiting_tr.source_req.completion_sent
                        and waiting_tr.source_req.status == REQUEST_STATUS_ERROR
                    ):
                        continue
                    waiting_tr.address = address
                    domain = self.domains[waiting_tr.source_req.sq_id]
                    if not domain.cmt.is_cached(lpa):
                        domain.cmt.add_entry(lpa, address, dirty=False)
                    else:
                        domain.cmt.update_entry(lpa, address, dirty=False)
                    self.tsu.Submit_trans(waiting_tr)
                self.waiting_for_mapping_trans[lpa].clear()
            self.tsu.Schedule()
        elif tr.type == TransactionType.MAPPING_WRITE:
            if not QUIET:
                debug_info(f"[AMU] <_handle_mapping_response> response tr: {repr(tr)}")
            if tr.mvpn not in self.gtd:
                self.gtd[tr.mvpn] = GTDEntry(address=tr.address, written=True)
            else:
                self.gtd[tr.mvpn].written = True
            leaving_lpa = []
            for i in range(len(tr.bitmap)):
                if tr.bitmap[i] == 0:
                    continue
                lpa = tr.mvpn * LPA_NO_PER_MAPPING_PAGE + i
                leaving_lpa.append(lpa)
            debug_info(f"[AMU] <_handle_mapping_response> leaving_lpa: {leaving_lpa}")
            for lpa in leaving_lpa:
                self.gmt.pop(lpa, None)
        debug_info(f"[AMU] <_handle_mapping_response> done")
        return
    
    def translate_and_submit(self, req: Request):
        # SEARCH and COMPUTE requests don't need to be translated
        if not QUIET:
            debug_info(f"[AMU] translate_and_submit: handling new request: {repr(req)}")
        if req.type in (RequestType.SEARCH, RequestType.COMPUTE, RequestType.STATIC_WRITE):
            self.tsu.Prepare_trans_submission()
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
            self.tsu.Schedule()
            return
        # process read and write requests
        domain = self.domains[req.sq_id]
        self.tsu.Prepare_trans_submission()
        if req.type == RequestType.READ:
            to_submit: list[Transaction] = []
            mapping_waits: list[tuple[int, Transaction, Transaction]] = []
            recorder = REQUEST_LATENCY_RECORDER()
            for tr in req.transaction_list:
                resolution = domain.query_cmt(tr)
                if resolution is not None:
                    if recorder is not None:
                        recorder.note_mapping_resolution(req, resolution)
                    if not QUIET:
                        debug_info(f"[AMU] <translate_and_submit> Cache hit for tr: {repr(tr)}")
                    to_submit.append(tr)
                else:
                    if recorder is not None:
                        recorder.note_mapping_resolution(req, "mapping_read")
                    if not QUIET:
                        debug_info(f"[AMU] <translate_and_submit> Cache miss for tr: {repr(tr)}")
                    mvpn = tr.lpa // LPA_NO_PER_MAPPING_PAGE
                    if mvpn not in self.gtd:
                        if self.block_manager.has_pending_host_write(tr.lpa):
                            self._defer_read_until_lpa_write_complete(tr)
                            continue
                        raise RequestFailure("Read request accessing non-existing mapping page")
                    entry = self.gtd[mvpn]
                    phy = self.tsu.phy
                    _addr = entry.address
                    _pd = phy._storage[_addr.channel][_addr.chip][_addr.die][_addr.plane][_addr.sub_plane][_addr.page]
                    idx = tr.lpa % LPA_NO_PER_MAPPING_PAGE
                    if len(_pd.valid_bitmap) == 0 or _pd.valid_bitmap[idx] == 0:
                        if self.block_manager.has_pending_host_write(tr.lpa):
                            self._defer_read_until_lpa_write_complete(tr)
                            continue
                        debug_info(f"[AMU] <translate_and_submit> lpa: {tr.lpa}, mvpn: {mvpn}, entry: {entry}")
                        raise RequestFailure("Read request accessing invalid lpa in mapping page")
                    if idx < len(_pd.data) and _pd.data[idx] == INVALID_PPA:
                        if self.block_manager.has_pending_host_write(tr.lpa):
                            self._defer_read_until_lpa_write_complete(tr)
                            continue
                        raise RequestFailure("Read request accessing invalid ppa in mapping page")
                    debug_info(f"[AMU] <translate_and_submit> Read mapping page")
                    read_tr = self.generate_mapping_read_transaction(tr, mvpn)
                    tr.rely_on_transactions.append(read_tr)
                    read_tr.required_by_transactions.append(tr)
                    mapping_waits.append((tr.lpa, tr, read_tr))
            for tr in to_submit:
                self.tsu.Submit_trans(tr)
            for lpa, waiting_tr, read_tr in mapping_waits:
                self.waiting_for_mapping_trans[lpa].append(waiting_tr)
                if recorder is not None:
                    recorder.note_mapping_wait_start(
                        waiting_tr.source_req,
                        str(id(read_tr)),
                        CURRENT_TIME(),
                    )
                self.tsu.Submit_trans(read_tr)
        elif req.type == RequestType.WRITE:
            """process write requests — with MQSim-style backpressure.

            Before allocating PPA, check free_block_pool.  If the pool is at or
            below STOP_SERVICING_WRITES_THRESHOLD, the transaction is queued in
            Block_Manager.waiting_writes and will be retried when GC returns a block.
            """
            for tr in req.transaction_list:
                plane_addr = self.get_plane_address_for_lpa(tr.lpa)
                tr.address = plane_addr
                tr._mapping_sq_id = req.sq_id
                try:
                    resolution, old_address = self._resolve_write_mapping_state(
                        req.sq_id,
                        tr.lpa,
                    )
                except ValueError as exc:
                    if tr.source_req is not None:
                        raise RequestFailure(str(exc))
                    raise
                is_overwrite = resolution != "unmapped"
                # ── Backpressure check (方案A / MQSim Stop_servicing_writes) ──
                # First-time writes preserve GC reserve space.  Overwrites usually
                # pass through to create invalid pages, except when GC can already
                # use the last frontier free page as relocation space.
                if is_overwrite:
                    if self.block_manager.should_backpressure_overwrite(plane_addr):
                        self.block_manager.enqueue_waiting_write(plane_addr, tr)
                        continue
                else:
                    if self.block_manager.should_backpressure_first_write(plane_addr):
                        self.block_manager.enqueue_waiting_write(plane_addr, tr)
                        continue  # skip PPA allocation, CMT update, and TSU submission
                page_address = self.block_manager.get_write_frontier(plane_addr)
                if page_address is None:
                    # Pool is genuinely empty — even overwrites must wait
                    self.block_manager.enqueue_waiting_write(plane_addr, tr)
                    continue
                tr.address = page_address
                self._apply_write_mapping_resolution(
                    req,
                    tr,
                    req.sq_id,
                    page_address,
                    resolution,
                    old_address,
                )
                self.tsu.Submit_trans(tr)
                self.block_manager._set_barrier(tr)
        # process search and compute requests, whose transaction ppa is decided in segment step
        elif req.type == RequestType.SEARCH:
            assert req.transaction_list is not None, "Search request transaction list is not set"
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
        elif req.type == RequestType.COMPUTE:
            assert req.transaction_list is not None, "Compute request transaction list is not set"
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
        elif req.type == RequestType.STATIC_WRITE:
            assert req.transaction_list is not None, "Static write request transaction list is not set"
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
                self.block_manager._set_barrier(tr)
        else:
            raise ValueError("Invalid request type for translate_and_submit")
        debug_info("[AMU] <translate_and_submit> Prepare trans submission complete")
        self.tsu.Schedule()
        debug_info("[AMU] <translate_and_submit> TSU Schedule complete")
        return
    
    def generate_mapping_write_transaction(self, cache: dict[int, cmt_entry], mvpn: int) -> None:
        debug_info(f"[AMU] <generate_mapping_write_transaction> writing back cache for mvpn: {mvpn}")
        self.tsu.Prepare_trans_submission()
        bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        data = [INVALID_PPA for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        lpa_to_eject = []
        for lpa, entry in cache.items():
            if lpa // LPA_NO_PER_MAPPING_PAGE != mvpn: # write back clear entry in the meantime
                continue
            index = lpa % LPA_NO_PER_MAPPING_PAGE
            bitmap[index] = 1
            data[index] = utils.translate_address_to_ppa(entry.address)
            self.gmt[lpa] = entry
            lpa_to_eject.append(lpa)
        for lpa in lpa_to_eject:
            self.cmt.eject_entry(lpa)
        if mvpn not in self.gtd:
            # writing to a new mapping page, get page address for it
            page_address = self.get_plane_address_for_mvpn(mvpn)
            self.gtd[mvpn] = GTDEntry(address=page_address, written=False)
            write_tr = Transaction(
                source_req=None,
                type=TransactionType.MAPPING_WRITE,
                mvpn=mvpn,
                address=page_address,
                payload=data,
                bitmap=bitmap,
                data_ready=True
            )
            self.tsu.Submit_trans(write_tr)
            self.block_manager._set_barrier(write_tr)
            self.latest_mapping_write[mvpn] = write_tr
        else:
            gtd_entry = self.gtd[mvpn]
            write_tr = Transaction(
                source_req=None,
                type=TransactionType.MAPPING_WRITE,
                mvpn=mvpn,
                address=gtd_entry.address,
                payload=data,
                bitmap=bitmap,
                data_ready=True
            )
            previous_write = self.latest_mapping_write.get(mvpn)
            if previous_write is not None and not previous_write.completed:
                previous_write.required_by_transactions.append(write_tr)
                write_tr.rely_on_transactions.append(previous_write)
            else:
                carry_over_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
                _phy = self.tsu.phy
                _gaddr = gtd_entry.address
                _gpd = _phy._storage[_gaddr.channel][_gaddr.chip][_gaddr.die][_gaddr.plane][_gaddr.sub_plane][_gaddr.page]
                _gpd_bitmap = _gpd.valid_bitmap if _gpd.function == PageType.MAPPING else []
                if len(_gpd_bitmap) > 0:
                    for i in range(LPA_NO_PER_MAPPING_PAGE):
                        old_valid = i < len(_gpd_bitmap) and _gpd_bitmap[i] == 1
                        if old_valid and bitmap[i] == 0:
                            carry_over_bitmap[i] = 1
                if any(carry_over_bitmap):
                    read_tr = self.generate_mapping_read_transaction(
                        write_tr,
                        mvpn,
                        access_bitmap=carry_over_bitmap,
                    )
                    read_tr.required_by_transactions.append(write_tr)
                    write_tr.rely_on_transactions.append(read_tr)
                    self.tsu.Submit_trans(read_tr)

            self.tsu.Submit_trans(write_tr)
            self.block_manager._set_barrier(write_tr)
            self.latest_mapping_write[mvpn] = write_tr
        self.tsu.Schedule()
        return
    def generate_mapping_read_transaction(
        self,
        trigger_tr: Transaction,
        mvpn,
        access_bitmap: list[int] | None = None,
    ) -> Transaction:
        mapping_page_address = self.gtd[mvpn].address
        if access_bitmap is None:
            access_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
            if trigger_tr.lpa != INVALID_LPA:
                access_bitmap[trigger_tr.lpa % LPA_NO_PER_MAPPING_PAGE] = 1
            else:
                # Mapping-write merge reads are now restricted to the specific
                # valid carry-over slots computed by generate_mapping_write_transaction.
                for i in range(min(LPA_NO_PER_MAPPING_PAGE, len(trigger_tr.bitmap))):
                    if trigger_tr.bitmap[i] == 0:
                        access_bitmap[i] = 1
        read_tr = Transaction(
            source_req=trigger_tr.source_req,
            type=TransactionType.MAPPING_READ,
            mvpn=mvpn,
            address=mapping_page_address,
            bitmap=list(access_bitmap),
        )
        return read_tr


    def get_plane_address_for_mvpn(self, mvpn) -> FlashAddress:
        """
        Return a fixed physical page address for mapping page mvpn.

        Mapping pages are reserved at the tail of random-access pages and are
        sequentially assigned: mvpn=0 -> first reserved page, mvpn=1 -> second, ...
        """
        if mvpn < 0 or mvpn >= self.mapping_page_count:
            raise ValueError(
                f"[AMU] mvpn {mvpn} out of range [0, {self.mapping_page_count - 1}]"
            )
        mapping_page_index = self.mapping_region_start_page + mvpn
        return self._random_access_page_index_to_address(mapping_page_index)

    def _random_access_page_index_to_address(self, page_index: int) -> FlashAddress:
        """Translate a linear page index in random-access region to full FlashAddress."""
        if page_index < 0 or page_index >= self.total_random_access_pages:
            raise ValueError(
                f"[AMU] random-access page index {page_index} out of range [0, {self.total_random_access_pages - 1}]"
            )

        g = self.flash_geometry
        non_static_chip_no = g.chip_per_channel - g.static_chip_per_channel
        pages_per_block = g.pages_per_block
        scheme = getattr(self, "_plane_allocation_scheme", "PAGE_LEVEL")
        if scheme == "CWDP":
            stripe = (
                g.channel_no
                * non_static_chip_no
                * g.dies
                * g.planes_per_die
            )
            channel_id = page_index % g.channel_no
            chip_id = (page_index // g.channel_no) % non_static_chip_no
            die_id = (page_index // (g.channel_no * non_static_chip_no)) % g.dies
            plane_id = (
                page_index
                // (g.channel_no * non_static_chip_no * g.dies)
            ) % g.planes_per_die
            offset_in_plane = page_index // stripe
            block_id = offset_in_plane // pages_per_block
            page_id = offset_in_plane % pages_per_block
            return FlashAddress(
                channel=channel_id,
                chip=chip_id,
                die=die_id,
                plane=plane_id,
                sub_plane=block_id,
                page=page_id,
            )

        pages_per_plane = g.blocks_per_plane * pages_per_block
        pages_per_die = g.planes_per_die * pages_per_plane
        pages_per_chip = g.dies * pages_per_die
        pages_per_channel = non_static_chip_no * pages_per_chip

        channel_id = page_index // pages_per_channel
        rem = page_index % pages_per_channel

        chip_id = rem // pages_per_chip
        rem = rem % pages_per_chip

        die_id = rem // pages_per_die
        rem = rem % pages_per_die

        plane_id = rem // pages_per_plane
        rem = rem % pages_per_plane

        block_id = rem // pages_per_block
        page_id = rem % pages_per_block

        return FlashAddress(
            channel=channel_id,
            chip=chip_id,
            die=die_id,
            plane=plane_id,
            sub_plane=block_id,
            page=page_id,
        )

    def get_plane_address_for_lpa(self, lpa) -> FlashAddress:
        if lpa < 0 or lpa >= self.random_access_data_pages:
            raise ValueError(
                f"[AMU] LPA {lpa} out of random-access data range [0, {self.random_access_data_pages - 1}] "
                f"(tail pages reserved for mapping pages)"
            )
        scheme = getattr(self, "_plane_allocation_scheme", "PAGE_LEVEL")
        if scheme == "CWDP":
            non_static_chip_no = CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL
            channel_id = lpa % CHANNEL_NO
            chip_id = (lpa // CHANNEL_NO) % non_static_chip_no
            die_id = (lpa // (non_static_chip_no * CHANNEL_NO)) % DIE_PER_CHIP
            plane_id = (lpa // (DIE_PER_CHIP * non_static_chip_no * CHANNEL_NO)) % PLANE_PER_DIE
        else:
            pages_per_plane = BLOCK_PER_PLANE * PAGE_PER_BLOCK
            lpa //= pages_per_plane
            plane_id = lpa % PLANE_PER_DIE
            lpa //= PLANE_PER_DIE
            die_id = lpa % DIE_PER_CHIP
            lpa //= DIE_PER_CHIP
            chip_id = lpa % CHIP_PER_CHANNEL
            channel_id = lpa // CHIP_PER_CHANNEL
        address = FlashAddress(
            channel=channel_id,
            chip=chip_id,
            die=die_id,
            plane=plane_id,
            sub_plane=-1,
            page=-1
        )
        return address

    def apply_gc_write_complete(self, tr: Transaction) -> None:
        """GC_WRITE 完成后：无效化旧物理页、更新 LPA 映射、标记新页有效。"""
        bm = self.block_manager
        lpa = tr.lpa
        old_addr = tr.gc_old_address
        new_addr = tr.address

        def invalidate_gc_target() -> None:
            target_bke = bm.get_block_bke(new_addr)
            if new_addr.page in target_bke.invalid_pages:
                return
            if new_addr.page not in target_bke.valid_pages:
                bm._mark_valid(new_addr, reserved=True)
            bm._mark_invalid(new_addr)

        try:
            resolution, current_addr = self._resolve_write_mapping_state(0, lpa)
        except ValueError:
            raise

        if resolution == "unmapped" or current_addr is None:
            invalidate_gc_target()
            return
        if old_addr is None or current_addr != old_addr:
            invalidate_gc_target()
            return

        old_bke = bm.get_block_bke(old_addr)
        if old_addr.page in old_bke.valid_pages:
            bm._mark_invalid(old_addr)
        if resolution == "cmt_hit":
            ent = self.cmt.cache[lpa]
            ent.address = new_addr
            ent.dirty = True
        elif resolution == "gmt_hit":
            self.gmt[lpa].address = new_addr
        else:
            self.cmt.add_entry(lpa, new_addr, dirty=True)
        bm._mark_valid(new_addr, reserved=True)

class GC_WL_Unit:
    def __init__(self):
        self._construction_valid: bool = False
        self.block_manager: Block_Manager
        self.tsu: TSU
        self.address_mapping_unit: Address_Mapping_Unit
        self.gc_low_watermark: int = GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD
        self.gc_exec_threshold: float | None = None
        self.gc_min_invalid_pages: int = 1
        self.gc_min_invalid_ratio: float = 0.0
        self.gc_emergency_watermark: int = 1
        self.gc_victim_policy: str = "greedy"
        self.gc_d_choices: int = 10
        self._gc_random = random.Random(42)
        self.static_wl_enabled: bool = True
        self.static_wl_wear_gap_threshold: int = 2

    def apply_runtime_config(self, runtime: RuntimeConfig) -> None:
        self.gc_low_watermark = runtime.gc_low_watermark
        self.gc_exec_threshold = runtime.gc_exec_threshold
        self.gc_min_invalid_pages = runtime.gc_min_invalid_pages
        self.gc_min_invalid_ratio = runtime.gc_min_invalid_ratio
        self.gc_emergency_watermark = runtime.gc_emergency_watermark
        self.gc_victim_policy = runtime.gc_victim_policy
        self.gc_d_choices = runtime.gc_d_choices
        self._gc_random = random.Random(runtime.gc_random_seed)
        self.static_wl_enabled = runtime.static_wl_enabled
        self.static_wl_wear_gap_threshold = runtime.static_wl_wear_gap_threshold
    
    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.block_manager is not None, "GC_WL_Unit block_manager is not set"
        assert self.tsu is not None, "GC_WL_Unit tsu is not set"
        assert self.address_mapping_unit is not None, "GC_WL_Unit address_mapping_unit is not set"
        self._construction_valid = True

    def select_wl_aware_free_block(
        self,
        plane_address: FlashAddress,
        *,
        prefer_highest_wl: bool = False,
        exclude_blocks: set[int] | None = None,
    ) -> int:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        exclude = set(exclude_blocks or ())
        exclude |= plane_bke.gc_wl_barrier_blocks
        exclude |= plane_bke.metadata_blocks
        candidates: list[int] = []
        for bid in sorted(plane_bke.free_block_pool):
            if bid in exclude:
                continue
            bke = plane_bke.block_entries[bid]
            if bke.valid_page_count != 0 or bke.invalid_page_count != 0:
                continue
            if bke.write_frontier != 0 or bke.free_page_count != PAGE_PER_BLOCK:
                continue
            candidates.append(bid)
        if not candidates:
            return -1
        if prefer_highest_wl:
            return min(candidates, key=lambda bid: (-plane_bke.block_entries[bid].wl_level, bid))
        return min(candidates, key=lambda bid: (plane_bke.block_entries[bid].wl_level, bid))

    def _block_has_inflight_user_program(self, plane_address: FlashAddress, block_id: int) -> bool:
        key = (
            plane_address.channel,
            plane_address.chip,
            plane_address.die,
            plane_address.plane,
            block_id,
        )
        return self.block_manager.user_program_block_counts.get(key, 0) > 0

    def _is_safe_maintenance_block(self, plane_address: FlashAddress, block_id: int) -> bool:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        if block_id == plane_bke.write_frontier_block:
            return False
        if block_id == plane_bke.gc_erase_barrier_block_id:
            return False
        if block_id in plane_bke.metadata_blocks:
            return False
        if block_id in plane_bke.gc_wl_barrier_blocks:
            return False
        if block_id in plane_bke.free_block_pool:
            return False
        if self._block_has_inflight_user_program(plane_address, block_id):
            return False
        return True

    def _is_safe_gc_destination_block(
        self,
        plane_address: FlashAddress,
        block_id: int,
        exclude_blocks: set[int],
        *,
        allow_write_frontier: bool = False,
    ) -> bool:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        if block_id in exclude_blocks:
            return False
        if not allow_write_frontier and block_id == plane_bke.write_frontier_block:
            return False
        if block_id == plane_bke.gc_erase_barrier_block_id:
            return False
        if block_id in plane_bke.metadata_blocks:
            return False
        if block_id in plane_bke.gc_wl_barrier_blocks:
            return False
        if self._block_has_inflight_user_program(plane_address, block_id):
            return False
        bke = plane_bke.block_entries[block_id]
        if bke.write_frontier >= self.block_manager.pages_per_block:
            return False
        if self.block_manager.pages_per_block - bke.write_frontier <= 0:
            return False
        return True

    def _pick_gc_destination_block(
        self,
        plane_address: FlashAddress,
        needed_pages: int,
        *,
        exclude_blocks: set[int] | None = None,
    ) -> int:
        exclude = set(exclude_blocks or ())
        clean_block = self.select_wl_aware_free_block(
            plane_address,
            exclude_blocks=exclude,
        )
        if clean_block >= 0:
            return clean_block

        plane_bke = self.block_manager.get_plane_bke(plane_address)
        def collect_candidates(*, allow_write_frontier: bool) -> list[int]:
            candidates: list[int] = []
            for bid, bke in enumerate(plane_bke.block_entries):
                if not self._is_safe_gc_destination_block(
                    plane_address,
                    bid,
                    exclude,
                    allow_write_frontier=allow_write_frontier,
                ):
                    continue
                remaining_slots = self.block_manager.pages_per_block - bke.write_frontier
                if remaining_slots < needed_pages:
                    continue
                candidates.append(bid)
            return candidates

        candidates = collect_candidates(allow_write_frontier=False)
        if not candidates:
            candidates = collect_candidates(allow_write_frontier=True)
        if not candidates:
            return -1
        return min(
            candidates,
            key=lambda bid: (
                self.block_manager.pages_per_block - plane_bke.block_entries[bid].write_frontier,
                plane_bke.block_entries[bid].wl_level,
                plane_bke.block_entries[bid].valid_page_count,
                bid,
            ),
        )

    def _pick_gc_victim_block(
        self,
        plane_address: FlashAddress,
        *,
        require_no_valid: bool = False,
        min_invalid_pages: int = 1,
    ) -> int:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        if self.gc_victim_policy == "d-choices":
            candidates = self._gc_victim_candidates(
                plane_address,
                require_no_valid=require_no_valid,
                min_invalid_pages=min_invalid_pages,
            )
            if not candidates:
                return -1
            sample_size = min(self.gc_d_choices, len(candidates))
            sampled = self._gc_random.sample(candidates, sample_size)
            victim = min(
                sampled,
                key=lambda bid: (
                    plane_bke.block_entries[bid].valid_page_count,
                    -plane_bke.block_entries[bid].invalid_page_count,
                    plane_bke.block_entries[bid].wl_level,
                    bid,
                ),
            )
            return victim

        candidates = self._gc_victim_candidates(
            plane_address,
            require_no_valid=require_no_valid,
            min_invalid_pages=min_invalid_pages,
        )
        best_block = -1
        best_key = None
        for bid in candidates:
            bke = plane_bke.block_entries[bid]
            key = (
                bke.invalid_page_count,
                -bke.valid_page_count,
                -bke.wl_level,
                -bid,
            )
            if best_key is None or key > best_key:
                best_key = key
                best_block = bid
        return best_block

    def _gc_victim_candidates(
        self,
        plane_address: FlashAddress,
        *,
        require_no_valid: bool,
        min_invalid_pages: int,
    ) -> list[int]:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        candidates: list[int] = []
        for bid in range(self.block_manager.block_no_per_plane):
            if not self._is_safe_maintenance_block(plane_address, bid):
                continue
            bke = plane_bke.block_entries[bid]
            if bke.invalid_page_count < min_invalid_pages:
                continue
            if require_no_valid and bke.valid_page_count > 0:
                continue
            candidates.append(bid)
        return candidates

    def _pick_static_wl_source_block(self, plane_address: FlashAddress) -> int:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        best_block = -1
        best_key = None
        for bid in range(self.block_manager.block_no_per_plane):
            if not self._is_safe_maintenance_block(plane_address, bid):
                continue
            bke = plane_bke.block_entries[bid]
            if bke.valid_page_count <= 0:
                continue
            key = (bke.wl_level, bke.invalid_page_count, bid)
            if best_key is None or key < best_key:
                best_key = key
                best_block = bid
        return best_block

    def _wear_skew_requires_static_wl(self, plane_address: FlashAddress) -> bool:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        wl_levels = [
            plane_bke.block_entries[bid].wl_level
            for bid in range(self.block_manager.block_no_per_plane)
        ]
        if not wl_levels:
            return False
        return max(wl_levels) - min(wl_levels) >= self.static_wl_wear_gap_threshold

    def _gc_min_invalid_pages_for_pressure(self, plane_address: FlashAddress) -> int:
        free_blocks = self.block_manager.get_free_pool_count(plane_address)
        if free_blocks <= self.gc_emergency_watermark:
            return 1
        ratio_pages = math.ceil(self.block_manager.pages_per_block * self.gc_min_invalid_ratio)
        return max(self.gc_min_invalid_pages, ratio_pages)

    def _gc_trigger_threshold_blocks(self) -> int:
        if self.gc_exec_threshold is None:
            return self.gc_low_watermark
        threshold = int(self.gc_exec_threshold * self.block_manager.block_no_per_plane)
        return max(1, threshold)

    def _gc_required_for_pool_size(self, free_blocks: int) -> bool:
        threshold = self._gc_trigger_threshold_blocks()
        if self.gc_exec_threshold is None:
            return free_blocks <= threshold
        return free_blocks < threshold

    def can_trigger_gc(self, plane_address: FlashAddress) -> bool:
        plane_bke = self.block_manager.get_plane_bke(plane_address)
        if plane_bke.gc_wl_barrier_blocks:
            return False
        victim = self._pick_gc_victim_block(
            plane_address,
            min_invalid_pages=self._gc_min_invalid_pages_for_pressure(plane_address),
        )
        if victim < 0:
            return False
        if plane_bke.block_entries[victim].valid_page_count <= 0:
            return True
        if self._pick_gc_destination_block(
            plane_address,
            plane_bke.block_entries[victim].valid_page_count,
            exclude_blocks={victim},
        ) >= 0:
            return True
        return self._pick_gc_victim_block(
            plane_address,
            require_no_valid=True,
            min_invalid_pages=1,
        ) >= 0

    def check_gc(self):
        debug_info(f"[GC] <check_gc> checking gc")
        for channel in range(CHANNEL_NO):
            for chip in range(CHIP_PER_CHANNEL):
                for die in range(DIE_PER_CHIP):
                    for plane in range(PLANE_PER_DIE):
                        plane_addr = FlashAddress(
                            channel=channel,
                            chip=chip,
                            die=die,
                            plane=plane,
                            sub_plane=-1,
                            page=-1,
                        )
                        plane_bke = self.block_manager.get_plane_bke(plane_addr)
                        if plane_bke.gc_wl_barrier_blocks:
                            continue
                        if self._gc_required_for_pool_size(len(plane_bke.free_block_pool)):
                            self.block_manager._record_plane_snapshot(plane_addr)
                            self._trigger_gc(plane_addr)

    def check_gc_for_plane(self, plane_addr: FlashAddress) -> None:
        """Check GC pressure only on the physical plane touched by a write."""
        plane_addr = FlashAddress(
            channel=plane_addr.channel,
            chip=plane_addr.chip,
            die=plane_addr.die,
            plane=plane_addr.plane,
            sub_plane=-1,
            page=-1,
        )
        plane_bke = self.block_manager.get_plane_bke(plane_addr)
        if plane_bke.gc_wl_barrier_blocks:
            return
        threshold = self._gc_trigger_threshold_blocks()
        plane_key = self.block_manager._plane_key(plane_addr)
        if self.block_manager.waiting_writes.get(plane_key):
            threshold = max(
                threshold,
                self.block_manager.STOP_SERVICING_WRITES_THRESHOLD
                + self.block_manager.GC_RESERVE_BLOCKS,
            )
        free_blocks = len(plane_bke.free_block_pool)
        if (
            (self.gc_exec_threshold is None and free_blocks <= threshold)
            or (self.gc_exec_threshold is not None and free_blocks < threshold)
        ):
            self.block_manager._record_plane_snapshot(plane_addr)
            self._trigger_gc(plane_addr)
    
    def _trigger_gc(self, addr: FlashAddress):
        debug_info(f"[GC] <trigger_gc> for plane: {addr}")
        erase_target = self._pick_gc_victim_block(
            addr,
            min_invalid_pages=self._gc_min_invalid_pages_for_pressure(addr),
        )
        if erase_target < 0:
            debug_info("[GC] <trigger_gc> No safe block with invalid pages found, skipping GC")
            return
        plane_bke = self.block_manager.get_plane_bke(addr)
        dest_block = -1
        if plane_bke.block_entries[erase_target].valid_page_count > 0:
            dest_block = self._pick_gc_destination_block(
                addr,
                plane_bke.block_entries[erase_target].valid_page_count,
                exclude_blocks={erase_target},
            )
            if dest_block < 0:
                zero_valid_target = self._pick_gc_victim_block(
                    addr,
                    require_no_valid=True,
                    min_invalid_pages=1,
                )
                if zero_valid_target < 0:
                    debug_info("[GC] <trigger_gc> No eligible destination block found, skipping GC")
                    return
                erase_target = zero_valid_target
        self._submit_relocation_chain(addr, erase_target, dest_block, "gc")

    def _lpa_for_physical_page(self, src: FlashAddress) -> int:
        phy = self.tsu.phy
        if phy is not None:
            pd = phy._storage[src.channel][src.chip][src.die][src.plane][src.sub_plane][src.page]
            if pd.lpa != INVALID_LPA:
                return pd.lpa
        ppa = utils.translate_address_to_ppa(src)
        amu = self.address_mapping_unit
        for lpa, ent in amu.gmt.items():
            if utils.translate_address_to_ppa(ent.address) == ppa:
                return lpa
        for lpa, ent in amu.cmt.cache.items():
            if utils.translate_address_to_ppa(ent.address) == ppa:
                return lpa
        raise ValueError(f"[GC] cannot resolve LPA for physical page {src}")

    def _submit_relocation_chain(
        self,
        addr: FlashAddress,
        erase_target: int,
        dest_block: int,
        reason: str,
    ) -> bool:
        plane_bke = self.block_manager.get_plane_bke(addr)
        erase_target_block = plane_bke.block_entries[erase_target]
        dest_bke = plane_bke.block_entries[dest_block] if dest_block >= 0 else None
        pages_to_move = sorted(erase_target_block.valid_pages)
        n_valid = len(pages_to_move)
        if n_valid > 0 and dest_bke is None:
            debug_info(f"[GC/WL] <{reason}> no destination block available for relocation")
            return False
        dest_free_slots = (
            self.block_manager.pages_per_block - dest_bke.write_frontier
            if dest_bke is not None
            else 0
        )
        if n_valid > 0 and dest_free_slots < n_valid:
            debug_info(f"[GC/WL] <{reason}> dest block {dest_block} insufficient free pages: need {n_valid}, have {dest_free_slots}")
            return False
        if n_valid > 0:
            debug_info(f"[GC/WL] <{reason}> migrating {n_valid} valid pages from block {erase_target} to block {dest_block}")
        ch, chip, die, pl = addr.channel, addr.chip, addr.die, addr.plane
        plane_addr = FlashAddress(channel=ch, chip=chip, die=die, plane=pl, sub_plane=-1, page=-1)
        resolved_pages: list[tuple[FlashAddress, int]] = []
        for page_id in pages_to_move:
            src = FlashAddress(
                channel=ch,
                chip=chip,
                die=die,
                plane=pl,
                sub_plane=erase_target,
                page=page_id,
            )
            resolved_pages.append((src, self._lpa_for_physical_page(src)))

        plane_bke.gc_erase_barrier_block_id = erase_target
        plane_bke.gc_wl_barrier_blocks = {erase_target}
        if dest_block >= 0:
            plane_bke.gc_wl_barrier_blocks.add(dest_block)
        self.tsu.Prepare_trans_submission()
        gc_writes: list[Transaction] = []
        for src, lpa in resolved_pages:
            sector_bitmap = [1] * SECTOR_PER_PAGE
            gc_read = Transaction(
                source_req=None,
                type=TransactionType.GC_READ,
                lpa=lpa,
                address=src,
                bitmap=sector_bitmap,
            )
            dst = self.block_manager.allocate_gc_write_page(plane_addr, dest_block)
            gc_write = Transaction(
                source_req=None,
                type=TransactionType.GC_WRITE,
                lpa=lpa,
                address=dst,
                bitmap=[1] * SECTOR_PER_PAGE,
                payload=[INVALID_DATA] * SECTOR_PER_PAGE,
                gc_old_address=src,
            )
            gc_read.required_by_transactions.append(gc_write)
            gc_write.rely_on_transactions.append(gc_read)
            self.tsu.Submit_trans(gc_read)
            self.tsu.Submit_trans(gc_write)
            self.block_manager._set_barrier(gc_write)
            gc_writes.append(gc_write)
        erase_addr = FlashAddress(channel=ch, chip=chip, die=die, plane=pl, sub_plane=erase_target, page=0)
        gc_erase = Transaction(
            source_req=None,
            type=TransactionType.GC_ERASE,
            lpa=-1,
            address=erase_addr,
            maintenance_reason=reason,
        )
        if gc_writes:
            for gw in gc_writes:
                gw.required_by_transactions.append(gc_erase)
                gc_erase.rely_on_transactions.append(gw)
        self.tsu.Submit_trans(gc_erase)
        recorder = REQUEST_LATENCY_RECORDER()
        if recorder is not None:
            recorder.note_gc_started(
                reason,
                addr,
                erase_target,
                valid_page_count=n_valid,
                invalid_page_count=erase_target_block.invalid_page_count,
            )
        self.tsu.Schedule()
        return True

    def _trigger_static_wl(self, addr: FlashAddress) -> bool:
        if not self.static_wl_enabled:
            return False
        source_block = self._pick_static_wl_source_block(addr)
        if source_block < 0:
            return False
        plane_bke = self.block_manager.get_plane_bke(addr)
        source_wl = plane_bke.block_entries[source_block].wl_level
        dest_block = self.select_wl_aware_free_block(
            addr,
            prefer_highest_wl=True,
            exclude_blocks={source_block},
        )
        if dest_block < 0:
            return False
        dest_wl = plane_bke.block_entries[dest_block].wl_level
        if dest_wl - source_wl < self.static_wl_wear_gap_threshold:
            return False
        return self._submit_relocation_chain(addr, source_block, dest_block, "static-wl")

    def on_erase_complete(self, addr: FlashAddress, *, reason: str = "gc") -> None:
        plane_addr = FlashAddress(
            channel=addr.channel,
            chip=addr.chip,
            die=addr.die,
            plane=addr.plane,
            sub_plane=-1,
            page=-1,
        )
        plane_bke = self.block_manager.get_plane_bke(plane_addr)
        if not self.static_wl_enabled:
            return
        if reason == "static-wl":
            return
        if plane_bke.gc_wl_barrier_blocks:
            return
        if self.block_manager.waiting_writes.get(self.block_manager._plane_key(plane_addr)):
            return
        if self._gc_required_for_pool_size(len(plane_bke.free_block_pool)):
            return
        if not self._wear_skew_requires_static_wl(plane_addr):
            return
        self._trigger_static_wl(plane_addr)

class FTL:
    def Validate_construction(self):
        if self._construction_valid:
            return
        if not QUIET:
            print("Validating FTL construction...")
        assert self.address_mapping_unit is not None, "FTL address_mapping_unit is not set"
        assert self.gc_wl_unit is not None, "FTL gc_wl_unit is not set"
        assert self.block_manager is not None, "FTL block_manager is not set"
        assert self.tsu is not None, "FTL tsu is not set"
        self._construction_valid = True
        self.address_mapping_unit.Validate_construction()
        self.gc_wl_unit.Validate_construction()
        self.block_manager.Validate_construction()
        self.tsu.Validate_construction()
        if not QUIET:
            print("FTL construction validation complete.")

    def __init__(self):
        if not QUIET:
            print("Initializing FTL...")
        self._construction_valid: bool = False
        self.address_mapping_unit = Address_Mapping_Unit()
        self.gc_wl_unit = GC_WL_Unit()
        self.block_manager = Block_Manager()
        self.tsu = TSU()
        self.tsu.block_manager = self.block_manager
        self.block_manager.gc_wl_unit = self.gc_wl_unit
        self.gc_wl_unit.block_manager = self.block_manager
        self.gc_wl_unit.tsu = self.tsu
        self.gc_wl_unit.address_mapping_unit = self.address_mapping_unit
        self.address_mapping_unit.block_manager = self.block_manager
        self.address_mapping_unit.tsu = self.tsu
        for domain in self.address_mapping_unit.domains:
            domain.tsu = self.tsu
        if not QUIET:
            print("FTL initialization complete.")

    def apply_runtime_config(self, runtime: RuntimeConfig) -> None:
        self.block_manager.apply_runtime_config(runtime)
        self.gc_wl_unit.apply_runtime_config(runtime)
        self.address_mapping_unit._plane_allocation_scheme = runtime.plane_allocation

    def handle_new_req(self, req: Request):
        self.address_mapping_unit.translate_and_submit(req)

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.address_mapping_unit is not None, "FTL address_mapping_unit is not set"
        assert self.gc_wl_unit is not None, "FTL gc_wl_unit is not set"
        assert self.block_manager is not None, "FTL block_manager is not set"
        assert self.tsu is not None, "FTL tsu is not set"
        self.tsu.Validate_construction()
        self._construction_valid = True
    
    def get_static_address(self, sub_plane_id: int) -> FlashAddress:
        sub_plane_id = sub_plane_id - STATIC_BASE_LHA
        sub_plane_address = sub_plane_id % (SL_PER_BLOCK * SSL_PER_SL * BLOCK_PER_PLANE)
        sub_plane_id //= SL_PER_BLOCK * SSL_PER_SL * BLOCK_PER_PLANE
        plane_address = sub_plane_id % PLANE_PER_DIE
        sub_plane_id //= PLANE_PER_DIE
        die_address = sub_plane_id % DIE_PER_CHIP
        sub_plane_id //= DIE_PER_CHIP
        chip_address = sub_plane_id % STATIC_CHIP_PER_CHANNEL + CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL
        sub_plane_id //= STATIC_CHIP_PER_CHANNEL
        channel_address = sub_plane_id % CHANNEL_NO
        address = FlashAddress(
            channel=channel_address,
            chip=chip_address,
            die=die_address,
            plane=plane_address,
            sub_plane=sub_plane_address,  # static address doesn't have block/page
            page=-1
        )
        return address
