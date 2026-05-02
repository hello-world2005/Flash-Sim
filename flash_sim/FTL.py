# -*- coding: utf-8 -*-
from dataclasses import dataclass
from collections import defaultdict
from dataclasses import field
from typing import Any
import random
from .common import *
from .PHY import PHY, PageType
from . import utils

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
        plane_bke = self.address_mapping_unit.block_manager.block_keeping_book[address.channel][address.chip][address.die][address.plane]
        bke = plane_bke.block_entries[address.sub_plane]
        if dirty and address.page in bke.valid_pages:
            self.address_mapping_unit.block_manager._mark_invalid(entry.address)
        entry.address = address
        debug_info(f"[CMT] <add_entry> updating entry: ({lpa}, {repr(entry)})")
        entry.dirty = dirty
        self.lru_list.remove(lpa)
        self.lru_list.insert(0, lpa)
        return invalidation_victim_address
    
    def eject_entry(self, lpa: int):
        self.lru_list.remove(lpa)
        leaving_entry = self.cache.pop(lpa)
        self.address_mapping_unit.gmt[lpa] = leaving_entry
        debug_info(f"[CMT] <eject_entry> ejecting entry: ({lpa}, {repr(leaving_entry)})")
            
    
    def add_entry(self, lpa: int, address: FlashAddress, dirty: bool) -> None:
        entry = cmt_entry(address=address, dirty=dirty)
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



class Block_Manager:
    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating Block Manager construction...")
        assert self.block_keeping_book is not None, "Block Manager block_keeping_book is not set"
        self._construction_valid = True
        print("Block Manager construction validation complete.")

    def __init__(self,
                 channel_no=CHANNEL_NO,
                 chip_no_per_channel=CHIP_PER_CHANNEL,
                 die_no_per_chip=DIE_PER_CHIP,
                 plane_no_per_die=PLANE_PER_DIE,
                 block_no_per_plane=BLOCK_PER_PLANE,
                 pages_per_block=PAGE_PER_BLOCK):
        print("Initializing Block Manager...")
        self.channel_no = channel_no
        self.chip_no_per_channel = chip_no_per_channel
        self.die_no_per_chip = die_no_per_chip
        self.plane_no_per_die = plane_no_per_die
        self.block_no_per_plane = block_no_per_plane
        self.pages_per_block = pages_per_block
        self._construction_valid = False
        self.lpa_protected_book = dict[int, Transaction]()
        self.mvpn_protected_book = dict[int, Transaction]()
        self.gc_wl_manager: GC_WL_Manager
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
        print("Block Manager initialization complete.")

    def get_write_frontier(self, plane_address: FlashAddress) -> FlashAddress:
        channel_id = plane_address.channel
        chip_id = plane_address.chip
        die_id = plane_address.die
        plane_id = plane_address.plane
        plane_bke = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
        block_id = plane_bke.write_frontier_block
        bke = plane_bke.block_entries[block_id]
        page_id = bke.write_frontier
        print(f"[Block Manager] <get_write_frontier> plane_address: {plane_address}, block {block_id}, page: {page_id}")
        bke.write_frontier += 1
        if bke.write_frontier == PAGE_PER_BLOCK:
            bke.write_frontier = 0
            plane_bke.write_frontier_block += 1
            if plane_bke.write_frontier_block == BLOCK_PER_PLANE:
                raise ValueError(f"[Block Manager] <get_write_frontier> plane {plane_id} is full, write frontier block overflow")
        # write frontier 前移时立即更新 free page count；free/valid page count 在写事务完成时更新
        address = FlashAddress(channel=channel_id, chip=chip_id, die=die_id, plane=plane_id, sub_plane=block_id, page=page_id)
        print(f"[Block Manager] plane {plane_address} free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")
        return address

    def allocate_gc_write_page(self, plane_address: FlashAddress, block_id: int) -> FlashAddress:
        """Allocate a page within a specific block (for GC migration target)."""
        channel_id = plane_address.channel
        chip_id = plane_address.chip
        die_id = plane_address.die
        plane_id = plane_address.plane
        plane_bke = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
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
    
    def _set_barrier(self, tr: Transaction):
        debug_info(f"[Block Manager] <_set_barrier> setting barrier for tr: {repr(tr)}")
        if tr.type in [TransactionType.USER_WRITE]:
            if tr.lpa not in self.lpa_protected_book:
                self.lpa_protected_book[tr.lpa] = tr
        elif tr.type in [TransactionType.GC_WRITE]:
            if tr.lpa not in self.lpa_protected_book:
                self.lpa_protected_book[tr.lpa] = tr
        elif tr.type == TransactionType.MAPPING_WRITE:
            if tr.mvpn not in self.mvpn_protected_book:
                self.mvpn_protected_book[tr.mvpn] = tr
        else:
            raise ValueError(f"[Block Manager] <_set_barrier> unknown transaction type: {tr.type}")
        

    def _on_transaction_serviced(self, tr: Transaction) -> None:
        """
        Handle transaction serviced event.
        1. remove barrier from protected book
        2. check gc
        3. update block keeping book
        """
        if tr.type in [TransactionType.USER_WRITE]:
            self.lpa_protected_book.pop(tr.lpa) if tr.lpa in self.lpa_protected_book else None
            self._mark_valid(tr.address)
            if tr.invalidate_target is not None:
                self._mark_invalid(tr.invalidate_target)
            self.gc_wl_manager.check_gc()
        elif tr.type == TransactionType.GC_WRITE:
            self.lpa_protected_book.pop(tr.lpa) if tr.lpa in self.lpa_protected_book else None
            self.gc_wl_manager.address_mapping_unit.apply_gc_write_complete(tr)
        elif tr.type == TransactionType.MAPPING_WRITE:
            self.mvpn_protected_book.pop(tr.mvpn) if tr.mvpn in self.mvpn_protected_book else None
        elif tr.type == TransactionType.GC_ERASE:
            self.lpa_protected_book.pop(tr.lpa) if tr.lpa in self.lpa_protected_book else None
            self.finalize_gc_erase(tr.address)

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
    
    def _mark_valid(self, addr: FlashAddress):
        debug_info(f"[Block Manager] <_mark_valid> {addr}")
        bke = self.get_block_bke(addr)
        plane_bke = self.get_plane_bke(addr)
        bke.free_page_count -= 1                # free page count is subtracted immediately when issue an write transaction
        plane_bke.free_page_count -= 1
        if bke.free_page_count == 0:
            plane_bke.free_block_pool.discard(addr.sub_plane)
        bke.valid_pages.add(addr.page)          # update valid_pages when write transaction completed
        bke.valid_page_count += 1
        plane_bke.valid_page_count += 1
        print(f"[Block Manager] <_mark_valid> plane_bke updated! {addr}, free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")

    def _mark_invalid(self, addr: FlashAddress):
        print(f"[Block Manager] <_mark_invalid> {addr}")
        bke = self.get_block_bke(addr)
        if addr.page not in bke.valid_pages:
            raise ValueError(f"[Block Manager] <_mark_invalid> address {addr} is not valid!")
        plane_bke = self.get_plane_bke(addr)
        bke.invalid_pages.add(addr.page)          # update invalid_pages when an update write transaction is issued
        bke.invalid_page_count += 1
        plane_bke.invalid_page_count += 1
        bke.valid_pages.remove(addr.page)
        bke.valid_page_count -= 1
        plane_bke.valid_page_count -= 1

    def finalize_gc_erase(self, addr: FlashAddress) -> None:
        """After GC_ERASE: reset BKE/plane counts, return block to free pool, clear PHY storage."""
        bke = self.get_block_bke(addr)
        plane_bke = self.get_plane_bke(addr)
        plane_bke.valid_page_count -= bke.valid_page_count
        plane_bke.invalid_page_count -= bke.invalid_page_count
        bke.valid_pages.clear()
        bke.invalid_pages.clear()
        bke.valid_page_count = 0
        bke.invalid_page_count = 0
        bke.free_page_count = PAGE_PER_BLOCK
        bke.write_frontier = 0
        bke.wl_level += 1
        plane_bke.free_page_count += PAGE_PER_BLOCK
        plane_bke.free_block_pool.add(addr.sub_plane)
        plane_bke.gc_erase_barrier_block_id = None
        phy = self.gc_wl_manager.tsu.phy
        if phy is not None:
            phy.clear_block_pages(addr)
        print(f"[Block Manager] <finalize_gc_erase> plane {addr.plane} block {addr.sub_plane} erased, free_block_num {len(plane_bke.free_block_pool)}, free_page_num {plane_bke.free_page_count}, valid_page_num {plane_bke.valid_page_count}, invalid_page_num {plane_bke.invalid_page_count}")

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

        if data_path is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            data_path = os.path.join(base, 'pre_data', 'precondition_data.json')

        print("\n" + "=" * 80)
        print("[Block Manager] Starting preconditioning phase (data-driven)...")
        print(f"[Block Manager] Loading precondition data from: {data_path}")
        print("=" * 80)
        with open(data_path, 'r') as f:
            precondition_data = json.load(f)

        # 获取 PHY 和 AMU 引用（由调用方传入或从 gc_wl_manager 取）
        if phy is None:
            try:
                phy = self.gc_wl_manager.tsu.phy
            except AttributeError:
                phy = None
        if amu is None:
            try:
                amu = self.gc_wl_manager.address_mapping_unit
            except AttributeError:
                amu = None
        if amu is None:
            raise RuntimeError("[Block Manager] preconditioning: AMU (address_mapping_unit) is required!")

        # 读取 geometry
        from .config import FlashGeometry
        geometry = FlashGeometry()
        valid_invalid_ratio = geometry.valid_invalid_ratio
        cmt_capacity = CMT_SIZE
        cmt_ratio = getattr(geometry, "preconditioning_cmt_ratio", 0.5)
        if not (0.0 < valid_invalid_ratio <= 1.0):
            valid_invalid_ratio = 0.5
        if not (0.0 < cmt_ratio <= 1.0):
            cmt_ratio = 0.5

        # 1. user page赋值（plane分组，排除mapping区域）
        user_lpa_set = set()
        user_lpa_to_ppa = dict()  # lpa -> FlashAddress
        lpa_item_map = {item['lpa']: item for item in precondition_data}
        # 只分配user page区域
        for lpa in lpa_item_map:
            try:
                addr = amu.get_plane_address_for_lpa(lpa)
            except Exception:
                continue  # 跳过mapping page区域
            user_lpa_set.add(lpa)
        # plane分组
        plane_page_data = defaultdict(list)
        for lpa in user_lpa_set:
            addr = amu.get_plane_address_for_lpa(lpa)
            plane_page_data[(addr.channel, addr.chip, addr.die, addr.plane)].append(lpa)

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
                        self._precondition_plane_from_data(
                            channel_id, chip_id, die_id, plane_id,
                            items, valid_invalid_ratio, phy, amu, user_lpa_to_ppa
                        )

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
        user_lpa_list = list(user_lpa_to_ppa.keys())
        random.shuffle(user_lpa_list)
        cmt_num = int(cmt_capacity * cmt_ratio)
        for lpa in user_lpa_list[:cmt_num]:
            addr = user_lpa_to_ppa[lpa]
            amu.cmt.add_entry(lpa, addr, dirty=False)

        print("[Block Manager] Preconditioning phase completed.")
        print("=" * 80 + "\n")

    def _is_static_chip(self, chip_id: int) -> bool:
        """判断 chip 是否为 static chip（用于 SEARCH/COMPUTE，从末尾分配）。"""
        return chip_id >= self.chip_no_per_channel - STATIC_CHIP_PER_CHANNEL

    def _precondition_plane_from_data(
        self,
        channel_id: int, chip_id: int, die_id: int, plane_id: int,
        items: list, valid_invalid_ratio: float, phy, amu, user_lpa_to_ppa=None
    ) -> None:
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
        num_page = len(items)
        if num_page == 0:
            return
        valid_per_full_block = max(1, int(self.pages_per_block * valid_invalid_ratio))
        num_invalid_per_full = self.pages_per_block - valid_per_full_block
        num_full_block = num_page // valid_per_full_block
        left_page = num_page % valid_per_full_block
        # Overfull 检查：full block + GC 阈值不能 >= block_per_plane（还需留 write_frontier_block）
        if num_full_block + GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD >= self.block_no_per_plane:
            raise ValueError(
                f"[Block Manager] Plane overfull! "
                f"Channel {channel_id} Chip {chip_id} Die {die_id} Plane {plane_id}: "
                f"num_full_block={num_full_block}, GC_threshold={GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD}, "
                f"block_per_plane={self.block_no_per_plane}. "
                f"Reduce num_data in precondition_data.json."
            )
        all_blocks = list(range(self.block_no_per_plane))
        full_blocks = set(random.sample(all_blocks, num_full_block)) if num_full_block > 0 else set()
        remaining_blocks = set(all_blocks) - full_blocks
        write_frontier_block_id = random.choice(list(remaining_blocks))
        remaining_blocks.remove(write_frontier_block_id)
        plane_bke.free_block_pool = remaining_blocks.copy()
        plane_bke.write_frontier_block = write_frontier_block_id
        data_idx = 0
        for block_id in full_blocks:
            bke = plane_bke.block_entries[block_id]
            bke.write_frontier = self.pages_per_block
            bke.free_page_count = 0
            all_pages = list(range(self.pages_per_block))
            random.shuffle(all_pages)
            valid_positions = all_pages[:valid_per_full_block]
            invalid_positions = all_pages[valid_per_full_block:]
            bke.valid_pages = set(valid_positions)
            bke.valid_page_count = valid_per_full_block
            bke.invalid_pages = set(invalid_positions)
            bke.invalid_page_count = num_invalid_per_full
            for page_idx in valid_positions:
                item = items[data_idx]
                data_idx += 1
                lpa = item['lpa']
                if phy is not None:
                    pd = phy._storage[channel_id][chip_id][die_id][plane_id][block_id][page_idx]
                    raw_valid_bitmap = item.get('valid_bitmap', [1] * SECTOR_PER_PAGE)
                    raw_data = item.get('data', [])
                    valid_bitmap = [0] * SECTOR_PER_PAGE
                    payload = [INVALID_DATA] * SECTOR_PER_PAGE
                    for i in range(min(SECTOR_PER_PAGE, len(raw_valid_bitmap))):
                        valid_bitmap[i] = 1 if raw_valid_bitmap[i] else 0
                    for i in range(min(SECTOR_PER_PAGE, len(raw_data))):
                        if valid_bitmap[i] == 1:
                            payload[i] = raw_data[i]
                    pd.lpa = lpa
                    pd.mvpn = INVALID_MVPN
                    pd.valid_bitmap = valid_bitmap
                    pd.data = payload
                    pd.function = PageType.USER
                if user_lpa_to_ppa is not None:
                    addr = FlashAddress(
                        channel=channel_id, chip=chip_id, die=die_id, plane=plane_id,
                        sub_plane=block_id, page=page_idx
                    )
                    user_lpa_to_ppa[lpa] = addr
            plane_bke.valid_page_count += valid_per_full_block
            plane_bke.invalid_page_count += num_invalid_per_full
        # 处理 write_frontier_block
        frontier_bke = plane_bke.block_entries[write_frontier_block_id]
        if left_page > 0:
            write_frontier_page = min(int(left_page / valid_invalid_ratio), self.pages_per_block)
            num_invalid_in_frontier = write_frontier_page - left_page
            used_positions = list(range(write_frontier_page))
            valid_positions_f = set(random.sample(used_positions, left_page))
            invalid_positions_f = set(p for p in used_positions if p not in valid_positions_f)
            frontier_bke.valid_pages = valid_positions_f
            frontier_bke.valid_page_count = left_page
            frontier_bke.invalid_pages = invalid_positions_f
            frontier_bke.invalid_page_count = num_invalid_in_frontier
            frontier_bke.free_page_count = self.pages_per_block - write_frontier_page
            frontier_bke.write_frontier = write_frontier_page
            for page_idx in valid_positions_f:
                item = items[data_idx]
                data_idx += 1
                lpa = item['lpa']
                if phy is not None:
                    pd = phy._storage[channel_id][chip_id][die_id][plane_id][write_frontier_block_id][page_idx]
                    raw_valid_bitmap = item.get('valid_bitmap', [1] * SECTOR_PER_PAGE)
                    raw_data = item.get('data', [])
                    valid_bitmap = [0] * SECTOR_PER_PAGE
                    payload = [INVALID_DATA] * SECTOR_PER_PAGE
                    for i in range(min(SECTOR_PER_PAGE, len(raw_valid_bitmap))):
                        valid_bitmap[i] = 1 if raw_valid_bitmap[i] else 0
                    for i in range(min(SECTOR_PER_PAGE, len(raw_data))):
                        if valid_bitmap[i] == 1:
                            payload[i] = raw_data[i]
                    pd.lpa = lpa
                    pd.mvpn = INVALID_MVPN
                    pd.valid_bitmap = valid_bitmap
                    pd.data = payload
                    pd.function = PageType.USER
                if user_lpa_to_ppa is not None:
                    addr = FlashAddress(
                        channel=channel_id, chip=chip_id, die=die_id, plane=plane_id,
                        sub_plane=write_frontier_block_id, page=page_idx
                    )
                    user_lpa_to_ppa[lpa] = addr
            plane_bke.valid_page_count += left_page
            plane_bke.invalid_page_count += num_invalid_in_frontier
        else:
            frontier_bke.free_page_count = self.pages_per_block
            frontier_bke.write_frontier = 0
        plane_bke.free_page_count = (
            len(plane_bke.free_block_pool) * self.pages_per_block
            + frontier_bke.free_page_count
        )
        debug_info(f"[Block Manager] <preconditioning> Channel {channel_id} Chip {chip_id} Die {die_id} Plane {plane_id}:")
        debug_info(f"  - Data entries: {num_page} | Full blocks: {num_full_block} | Left pages: {left_page}")
        debug_info(f"  - Write frontier block: {write_frontier_block_id} (frontier: {frontier_bke.write_frontier}/{self.pages_per_block})")
        debug_info(f"  - Free block pool: {len(plane_bke.free_block_pool)}")
        debug_info(f"  - Total free/valid/invalid pages: {plane_bke.free_page_count}/{plane_bke.valid_page_count}/{plane_bke.invalid_page_count}")

class TSU:
    """Transaction Scheduling Unit — Out-of-Order version.

    对标 MQSim TSU_OutOfOrder。管理 9 级调度队列，以 channel 为单位轮询 chip，
    按 读 > 写 > 擦除 的优先级向 PHY 下发命令。
    """

    def __init__(self):
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
        # deques[channel][chip][type] = list of Transaction
        self.queues = [
            [{key: [] for key in self.sched_priority}
             for _ in range(CHIP_PER_CHANNEL)]
            for _ in range(CHANNEL_NO)
        ]
        self.block_manager: Block_Manager
        self.channel_no = CHANNEL_NO
        self.chip_no_per_channel = CHIP_PER_CHANNEL
        self.round_robin_turn = [0] * self.channel_no
        self.phy : PHY
        # Register channel/chip idle callbacks so PHY can trigger re-scheduling
        # self.phy.connect_channel_idle_signal(self._on_channel_idle)
        # self.phy.connect_chip_idle_signal(self._on_chip_idle)
        self._construction_valid: bool = False
        print("TSU initialization complete.")
    
    def _reschedule(self, tr: Transaction):
        self.Prepare_trans_submission()
        debug_info(f"[TSU] <_reschedule> transaction serviced, rescheduling: {repr(tr)}")
        self.Schedule()
        return

    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating TSU construction...")
        assert self.block_manager is not None, "TSU block_manager is not set"
        assert self.phy is not None, "TSU PHY is not set"
        self._construction_valid = True
        self.block_manager.Validate_construction()
        self.phy.Validate_construction()
        print("TSU construction validation complete.")

    # ── Batch submission API ──────────────────────────────────────────────────

    def Prepare_trans_submission(self):
        """Open a submission batch; must be paired with Schedule()."""
        self._onfly_schedule_req_no += 1
        debug_info(f"[TSU] <Prepare_trans_submission> {self._onfly_schedule_req_no}")

    def Submit_trans(self, trans: Transaction):
        """Endeque a transaction to the appropriate per-chip priority deque."""
        debug_info(f"[TSU] <Submit_trans> submitting trans: {repr(trans)}")
        channel = trans.address.channel
        chip    = trans.address.chip
        self.queues[channel][chip][trans.type].append(trans)

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
            return True
        if tr.type in (
            TransactionType.USER_WRITE,
            TransactionType.MAPPING_WRITE,
            TransactionType.GC_WRITE,
            TransactionType.USER_STATIC_WRITE,
        ):
            addr = tr.address
            if addr.sub_plane >= 0:
                plane_bke = bm.get_plane_bke(addr)
                bid = plane_bke.gc_erase_barrier_block_id
                if bid is not None and addr.sub_plane == bid:
                    return True
        return False

    def Schedule(self):
        """Close batch and, if all batches are closed, drive scheduling.
        对标 TSU_OutOfOrder::Schedule()。
        """
        self._onfly_schedule_req_no -= 1
        debug_info(f"[TSU] <Schedule> {self._onfly_schedule_req_no}")
        if self._onfly_schedule_req_no < 0:
            raise RuntimeError("onfly_schedule_req_no should not be negative")
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
        # Priority: if any chip in this channel has completed a read but is still
        # holding data (waiting_data_out non-empty), start the data-out transfer
        # immediately before handing the channel to the TSU for new commands.
        # This mirrors MQSim's logic of checking WaitingReadTXCount in Execute_simulator_event.
        debug_info(f"[TSU] <_on_channel_idle> handling channel {channel_id} idle")
        for chip_no in range(CHIP_PER_CHANNEL):
            chip_id_check = (channel_id, chip_no)
            if chip_id_check not in self.phy._chip_bkes:
                raise ValueError(f"Chip {chip_id_check} not found in PHY while broadcasting channel idle")
            bke = self.phy._chip_bkes[chip_id_check]
            if not bke._has_data_waiting:
                continue
            debug_info(f"[TSU] <_on_channel_idle> chip {chip_id_check} has data waiting")
            for die_no in range(DIE_PER_CHIP):
                die_bke = bke.get_die_bke(die_no)
                if die_bke.active_command:
                    debug_info(f"[TSU] <_on_channel_idle> die {die_no} sending data out")
                    op = die_bke.active_command.cmd_type
                    transactions = die_bke.active_command.transactions
                    self.phy._transfer_data(chip_id_check, die_no, op, transactions)
            return
        debug_info(f"[TSU] <_on_channel_idle> no chip has data waiting, trying to activate chips")
        for _ in range(self.chip_no_per_channel):
            chip_id = (channel_id, self.round_robin_turn[channel_id])
            self.try_activate(chip_id)
            self.round_robin_turn[channel_id] = (
                (self.round_robin_turn[channel_id] + 1) % self.chip_no_per_channel
            )
            if self.channel_is_busy(channel_id):
                debug_info(f"[TSU] <_on_channel_idle> channel {channel_id} is busy, moving to next chip")
                break

    def _on_chip_idle(self, chip_id):
        """对标 handle_chip_idle_signal(): chip 空闲且 channel 空闲时尝试激活。"""
        channel_id = chip_id[0]
        debug_info(f"[TSU] <_on_chip_idle> handling chip {chip_id} idle")
        if not self.channel_is_busy(channel_id):
            self.try_activate(chip_id)

    # ── Top-level dispatcher ──────────────────────────────────────────────────

    def try_activate(self, chip_id) -> bool:
        """对标 process_chip_requests(): 按 读>写>擦除 顺序尝试激活 chip。"""
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
        for key in self.sched_priority:
            if key not in [TransactionType.MAPPING_READ, TransactionType.USER_READ, TransactionType.GC_READ]:
                continue
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
        for key in self.sched_priority:
            if key not in [TransactionType.USER_WRITE, TransactionType.GC_WRITE, TransactionType.MAPPING_WRITE]:
                continue
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

            for tr in list(q1):
                if tr.rely_on_transactions:
                    debug_info(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    debug_info(f"[TSU] <issue_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
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
                if len(dispatch_slots) >= plane_no:
                    break

            if q2 is not None and len(dispatch_slots) < plane_no:
                for tr in list(q2):
                    if tr.rely_on_transactions:
                        debug_info(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                        continue
                    if not tr.data_ready:
                        debug_info(f"[TSU] <issue_command> tr data not ready, skipping {repr(tr)}")
                        continue
                    if self._transaction_blocked_by_barrier(tr):
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
                    if len(dispatch_slots) >= plane_no:
                        break

            if dispatch_slots:
                dispatched = True
                debug_info(f"[TSU] <issue_command> dispatching {len(dispatch_slots)} transactions to PHY")
                for tr in dispatch_slots:
                    if tr in q1:
                        q1.remove(tr)
                    elif q2 is not None and tr in q2:
                        q2.remove(tr)
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

            for tr in list(q):
                if tr.rely_on_transactions:
                    debug_info(f"[TSU] <issue_search_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    debug_info(f"[TSU] <issue_search_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    debug_info(f"[TSU] <issue_search_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                if tr.address.die != die_id:
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
                    debug_info(f"[TSU] <issue_search_command> tr plane already selected, skipping {repr(tr)}")
                    continue
                # surppose 
                tr.SuspendRequired = False
                plane_vector |= 1 << tr_plane
                dispatch_slots.append(tr)
                if bin(plane_vector).count("1") >= plane_no:
                    break

            if dispatch_slots:
                for tr in dispatch_slots:
                    q.remove(tr)
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

            for tr in list(q):
                if tr.address.die != die_id:
                    continue
                if tr.rely_on_transactions:
                    debug_info(f"[TSU] <issue_compute_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    debug_info(f"[TSU] <issue_compute_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    debug_info(f"[TSU] <issue_compute_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                tr_plane = tr.address.plane
                count = plane_count.get(tr_plane, 0)
                if count >= max_per_plane:
                    continue
                tr.SuspendRequired = False
                plane_count[tr_plane] = count + 1
                dispatch_slots.append(tr)

            if dispatch_slots:
                for tr in dispatch_slots:
                    q.remove(tr)
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

            for tr in list(q):
                if tr.address.die != die_id:
                    continue
                if tr.rely_on_transactions:
                    debug_info(f"[TSU] <issue_static_write_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    debug_info(f"[TSU] <issue_static_write_command> tr data not ready, skipping {repr(tr)}")
                    continue
                if self._transaction_blocked_by_barrier(tr):
                    debug_info(f"[TSU] <issue_static_write_command> tr blocked by barrier, skipping {repr(tr)}")
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
                    debug_info(f"[TSU] <issue_static_write_command> tr plane already selected, skipping {repr(tr)}")
                    continue
                tr.SuspendRequired = False
                plane_vector |= 1 << tr_plane
                dispatch_slots.append(tr)
                if bin(plane_vector).count("1") >= plane_no:
                    break

            if dispatch_slots:
                for tr in dispatch_slots:
                    q.remove(tr)
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
        print("Validating Address Mapping Domain construction...")
        assert self.cmt is not None, "Address Mapping Domain cmt is not set"
        assert self.gmt is not None, "Address Mapping Domain gmt is not set"
        self._construction_valid = True
        print("Address Mapping Domain construction validation complete.")

    def query_cmt(self, transaction: Transaction) -> bool:
        if self.cmt.is_cached(transaction.lpa):
            entry = self.cmt.get_cached_entry(transaction.lpa)
            transaction.address = entry.address
            return True
        if transaction.lpa in self.gmt:
            entry = self.gmt[transaction.lpa]
            transaction.address = entry.address
            return True
        return False

class Address_Mapping_Unit:
    def Validate_construction(self):
        if self._construction_valid:
            return
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
        print("Address Mapping Unit construction validation complete.")

    def __init__(self):
        print("Initializing Address Mapping Unit...")
        self._construction_valid: bool = False
        self.flash_geometry = FlashGeometry()
        self.domains = [Address_Mapping_Domain() for _ in range(NUM_OF_QUEUES)]
        self.waiting_for_mapping_trans: dict[int, list[Transaction]] = defaultdict(list)
        self.tsu: TSU
        self.cmt: CMT
        self.gmt: dict[int, cmt_entry] = {}
        self.gtd: dict[int, GTDEntry] = {}
        self.block_manager: Block_Manager
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
    
    def _handle_mapping_response(self, tr: Transaction):
        # handle response for tr waiting mapping info
        if tr.type == TransactionType.MAPPING_READ:
            debug_info(f"[AMU] <_handle_mapping_response> response tr: {repr(tr)}")
            self.tsu.Prepare_trans_submission()
            if tr.response is None:
                raise ValueError("[AMU] <_handle_mapping_response> empty mapping read response")
            # get arriving lpa in the finished mapping read transaction
            arriving_lpa = []
            for i in range(len(tr.bitmap)):
                if tr.bitmap[i] == 0:
                    continue
                lpa = i + tr.mvpn * LPA_NO_PER_MAPPING_PAGE
                arriving_lpa.append(lpa) 
            debug_info(f"[AMU] <_handle_mapping_response> arriving_lpa: {arriving_lpa}, response: {tr.response}")
            # submit the waiting transactions for the arriving lpa, and update the cmt meanwhile
            for lpa in arriving_lpa:
                idx = lpa % LPA_NO_PER_MAPPING_PAGE
                if tr.response.valid_bitmap[idx] == 0:
                    raise ValueError(f"[AMU] <_handle_mapping_response> invalid lpa in mapping response, lpa={lpa}")
                ppa = tr.response.data[idx]
                if ppa == INVALID_PPA:
                    raise ValueError(f"[AMU] <_handle_mapping_response> invalid ppa in mapping response, lpa={lpa}")
                address = utils.translate_ppa_to_address(ppa)
                # add entry in cmt for host read path only
                if tr.source_req is not None and tr.source_req.sq_id is not None:
                    domain = self.domains[tr.source_req.sq_id]
                    if not domain.cmt.is_cached(lpa):
                        domain.cmt.add_entry(lpa, address, dirty=False)
                waiting_trs = self.waiting_for_mapping_trans[lpa]
                debug_info(f"[AMU] <_handle_mapping_response> number of waiting trs: {len(waiting_trs)}")
                for waiting_tr in waiting_trs:
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
            debug_info(f"[AMU] <_handle_mapping_response> response tr: {repr(tr)}")
            leaving_lpa = []
            for i in range(len(tr.bitmap)):
                if tr.bitmap[i] == 0:
                    continue
                lpa = tr.mvpn * LPA_NO_PER_MAPPING_PAGE + i
                leaving_lpa.append(lpa)
            debug_info(f"[AMU] <_handle_mapping_response> leaving_lpa: {leaving_lpa}")
            for lpa in leaving_lpa:
                self.gmt.pop(lpa)
        debug_info(f"[AMU] <_handle_mapping_response> done")
        return
    
    def translate_and_submit(self, req: Request):
        # SEARCH and COMPUTE requests don't need to be translated
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
            for tr in req.transaction_list:
                if domain.query_cmt(tr):
                    debug_info(f"[AMU] <translate_and_submit> Cache hit for tr: {repr(tr)}")
                    self.tsu.Submit_trans(tr)
                else:
                    debug_info(f"[AMU] <translate_and_submit> Cache miss for tr: {repr(tr)}")
                    self.waiting_for_mapping_trans[tr.lpa].append(tr)
                    mvpn = tr.lpa // LPA_NO_PER_MAPPING_PAGE
                    if mvpn not in self.gtd:
                        raise ValueError("Read request accessing non-existing mapping page")
                    entry = self.gtd[mvpn]
                    phy = self.tsu.phy
                    _addr = entry.address
                    _pd = phy._storage[_addr.channel][_addr.chip][_addr.die][_addr.plane][_addr.sub_plane][_addr.page]
                    if _pd.valid_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] == 0:
                        debug_info(f"[AMU] <translate_and_submit> lpa: {tr.lpa}, mvpn: {mvpn}, entry: {entry}")
                        raise ValueError("Read request accessing invalid lpa in mapping page")
                    debug_info(f"[AMU] <translate_and_submit> Read mapping page")
                    read_tr = self.generate_mapping_read_transaction(tr, mvpn)
                    tr.rely_on_transactions.append(read_tr)
                    read_tr.required_by_transactions.append(tr)
                    self.tsu.Submit_trans(read_tr)
        elif req.type == RequestType.WRITE:
            """process write requests
            for each tr in the write request, we need to:
            1. get a mapping page of this tr
            2. udpate the mapping page of this tr in cmt/gmt/gtd
            """
            for tr in req.transaction_list:
                page_address = self.get_plane_address_for_lpa(tr.lpa)
                page_address = self.block_manager.get_write_frontier(page_address)
                tr.address = page_address
                domain = self.domains[req.sq_id]
                if not domain.cmt.is_cached(tr.lpa):
                    domain.cmt.add_entry(tr.lpa, page_address, dirty=True) # dirty is true because a write tr must update the ppa of a lpa
                else:
                    invalidation_victim_address = domain.cmt.update_entry(tr.lpa, page_address, dirty=True)
                    tr.invalidate_target = invalidation_victim_address
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
            self.gtd[mvpn] = GTDEntry(address=page_address)
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
            need_read = False
            _phy = self.tsu.phy
            _gaddr = gtd_entry.address
            _gpd = _phy._storage[_gaddr.channel][_gaddr.chip][_gaddr.die][_gaddr.plane][_gaddr.sub_plane][_gaddr.page]
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if _gpd.valid_bitmap[i] == 1 and bitmap[i] == 0:
                    need_read = True
                    break
            # working
            if need_read:
                read_tr = self.generate_mapping_read_transaction(write_tr, mvpn)
                read_tr.required_by_transactions.append(write_tr)
                write_tr.rely_on_transactions.append(read_tr)
                self.tsu.Submit_trans(read_tr)

            self.tsu.Submit_trans(write_tr)
            self.block_manager._set_barrier(write_tr)
        self.tsu.Schedule()
        return
    def generate_mapping_read_transaction(self, trigger_tr: Transaction, mvpn) -> Transaction:
        mapping_page_address = self.gtd[mvpn].address
        access_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        if trigger_tr.lpa != INVALID_LPA:
            access_bitmap[trigger_tr.lpa % LPA_NO_PER_MAPPING_PAGE] = 1
        else:
            # For mapping write merge, read old entries that are not overwritten in this write.
            for i in range(min(LPA_NO_PER_MAPPING_PAGE, len(trigger_tr.bitmap))):
                if trigger_tr.bitmap[i] == 0:
                    access_bitmap[i] = 1
        read_tr = Transaction(
            source_req=trigger_tr.source_req,
            type=TransactionType.MAPPING_READ,
            mvpn=mvpn,
            address=mapping_page_address,
            bitmap=access_bitmap
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
        # LPA 从低到高: page in block, block in plane, plane in die, die, chip, channel.
        # 先除以 (block*page) 再对 PLANE_PER_DIE 取模，得到 plane 索引 [0, PLANE_PER_DIE-1]
        if lpa < 0 or lpa >= self.random_access_data_pages:
            raise ValueError(
                f"[AMU] LPA {lpa} out of random-access data range [0, {self.random_access_data_pages - 1}] "
                f"(tail pages reserved for mapping pages)"
            )
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
        if old_addr is not None:
            old_bke = bm.get_block_bke(old_addr)
            if old_addr.page in old_bke.valid_pages:
                bm._mark_invalid(old_addr)
        if self.cmt.is_cached(lpa):
            ent = self.cmt.cache[lpa]
            ent.address = new_addr
            ent.dirty = True
        elif lpa in self.gmt:
            self.gmt[lpa].address = new_addr
        bm._mark_valid(new_addr)

class GC_WL_Manager:
    def __init__(self):
        self._construction_valid: bool = False
        self.block_manager: Block_Manager
        self.tsu: TSU
        self.address_mapping_unit: Address_Mapping_Unit
    
    def Validate_construction(self):
        pass

    def check_gc(self):
        debug_info(f"[GC] <check_gc> checking gc")
        for channel in range(CHANNEL_NO):
            for chip in range(CHIP_PER_CHANNEL):
                for die in range(DIE_PER_CHIP):
                    for plane in range(PLANE_PER_DIE):
                        plane_bke = self.block_manager.get_plane_bke(FlashAddress(channel=channel, chip=chip, die=die, plane=plane, sub_plane=-1, page=-1))
                        if len(plane_bke.free_block_pool) <= GC_WL_MANAGER_FREE_BLOCK_POOL_THRESHOLD:
                            self._trigger_gc(FlashAddress(channel=channel, chip=chip, die=die, plane=plane, sub_plane=-1, page=-1))
        pass
    
    def _trigger_gc(self, addr: FlashAddress):
        print(f"[GC] <trigger_gc> for plane: {addr}")
        plane_bke = self.block_manager.get_plane_bke(addr)
        max_invalid_page_count = 0
        max_invalid_page_count_block = -1
        min_wl_level = float('inf')
        min_wl_level_block = -1
        for block in range(BLOCK_PER_PLANE):
            bke = plane_bke.block_entries[block]
            if bke.invalid_page_count > max_invalid_page_count:
                max_invalid_page_count = bke.invalid_page_count
                max_invalid_page_count_block = block
            if bke.wl_level < min_wl_level:
                min_wl_level = bke.wl_level
                min_wl_level_block = block
        if max_invalid_page_count_block == -1:
            print("[GC] <trigger_gc> No block with invalid page found! Erasing block_id 0")
            max_invalid_page_count_block = 0
        self._gc(addr, max_invalid_page_count_block, min_wl_level_block)

    def _pick_alternate_dest_block(self, plane_bke: PlaneBKE, erase_target: int) -> int:
        best_bid = -1
        best_wl = float("inf")
        for bid in range(BLOCK_PER_PLANE):
            if bid == erase_target:
                continue
            bke = plane_bke.block_entries[bid]
            if bke.wl_level < best_wl:
                best_wl = bke.wl_level
                best_bid = bid
        if best_bid < 0:
            return (erase_target + 1) % BLOCK_PER_PLANE
        return best_bid

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

    def _gc(self, addr: FlashAddress, erase_target: int, erase_write_frontier: int):
        plane_bke = self.block_manager.get_plane_bke(addr)
        erase_target_block = plane_bke.block_entries[erase_target]
        dest_block = erase_write_frontier
        if dest_block == erase_target:
            dest_block = self._pick_alternate_dest_block(plane_bke, erase_target)
        dest_bke = plane_bke.block_entries[dest_block]
        pages_to_move = list(erase_target_block.valid_pages)
        n_valid = len(pages_to_move)
        if n_valid > 0 and dest_bke.free_page_count < n_valid:
            print(f"[GC] <_gc> dest block {dest_block} insufficient free pages: need {n_valid}, have {dest_bke.free_page_count}")
            return
        if n_valid > 0:
            print(f"[GC] <_gc> migrating {n_valid} valid pages from block {erase_target} to block {dest_block}")
        ch, chip, die, pl = addr.channel, addr.chip, addr.die, addr.plane
        plane_addr = FlashAddress(channel=ch, chip=chip, die=die, plane=pl, sub_plane=-1, page=-1)
        plane_bke.gc_erase_barrier_block_id = erase_target
        self.tsu.Prepare_trans_submission()
        gc_writes: list[Transaction] = []
        for page_id in pages_to_move:
            src = FlashAddress(channel=ch, chip=chip, die=die, plane=pl, sub_plane=erase_target, page=page_id)
            lpa = self._lpa_for_physical_page(src)
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
        )
        if gc_writes:
            for gw in gc_writes:
                gw.required_by_transactions.append(gc_erase)
                gc_erase.rely_on_transactions.append(gw)
        self.tsu.Submit_trans(gc_erase)
        self.tsu.Schedule()

class FTL:
    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating FTL construction...")
        assert self.address_mapping_unit is not None, "FTL address_mapping_unit is not set"
        assert self.gc_wl_manager is not None, "FTL gc_wl_manager is not set"
        assert self.block_manager is not None, "FTL block_manager is not set"
        assert self.tsu is not None, "FTL tsu is not set"
        self._construction_valid = True
        self.address_mapping_unit.Validate_construction()
        self.gc_wl_manager.Validate_construction()
        self.block_manager.Validate_construction()
        self.tsu.Validate_construction()
        print("FTL construction validation complete.")

    def __init__(self):
        print("Initializing FTL...")
        self._construction_valid: bool = False
        self.address_mapping_unit = Address_Mapping_Unit()
        self.gc_wl_manager = GC_WL_Manager()
        self.block_manager = Block_Manager()
        self.tsu = TSU()
        self.tsu.block_manager = self.block_manager
        self.block_manager.gc_wl_manager = self.gc_wl_manager
        self.gc_wl_manager.block_manager = self.block_manager
        self.gc_wl_manager.tsu = self.tsu
        self.gc_wl_manager.address_mapping_unit = self.address_mapping_unit
        self.address_mapping_unit.block_manager = self.block_manager
        self.address_mapping_unit.tsu = self.tsu
        for domain in self.address_mapping_unit.domains:
            domain.tsu = self.tsu
        print("FTL initialization complete.")

    def handle_new_req(self, req: Request):
        self.address_mapping_unit.translate_and_submit(req)

    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.address_mapping_unit is not None, "FTL address_mapping_unit is not set"
        assert self.gc_wl_manager is not None, "FTL gc_wl_manager is not set"
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
