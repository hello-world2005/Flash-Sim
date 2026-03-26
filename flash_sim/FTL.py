# -*- coding: utf-8 -*-
from dataclasses import dataclass
from doctest import debug
from nt import access
from typing import Mapping
from pathlib import Path
import json
from collections import defaultdict
from dataclasses import field

from .common import *
from .PHY import PHY
from . import utils
from collections import deque as deque

class CMT:
    def __init__(self):
        self.cache: dict[int, cmt_entry] = field(default_factory=dict)
        self.lru_list: list[int] = field(default_factory=list)
        self.address_mapping_unit: Address_Mapping_Unit

    def is_cached(self, lpa: int) -> bool:
        return lpa in self.cache

    def get_cached_entry(self, lpa: int) -> cmt_entry:
        self.lru_list.remove(lpa)
        self.lru_list.insert(0, lpa)
        return self.cache[lpa]

    def udpate_entry(self, lpa: int, address: FlashAddress, dirty: bool):
        entry = self.cache[lpa]
        entry.address = address
        entry.dirty = dirty
        self.lru_list.remove(lpa)
        self.lru_list.insert(0, lpa)
        return
    
    def eject_entry(self, lpa: int = None) -> tuple[int, cmt_entry]:
        if lpa is None:
            # ejecting least recently used entry
            lru_lpa = self.lru_list.pop()
            leaving_entry = (lru_lpa, self.cache.pop(lru_lpa))
            debug_info(f"[CMT] <eject_entry> ejecting entry: ({lru_lpa}, {repr(leaving_entry[1])})")
            return leaving_entry
        else:
            lru_lpa = lpa
            self.lru_list.remove(lpa)
            leaving_entry = (lpa, self.cache.pop(lpa))
            debug_info(f"[CMT] <eject_entry> ejecting entry: ({lpa}, {repr(leaving_entry[1])})")
            return leaving_entry
    
    def add_entry(self, lpa: int, address: FlashAddress, dirty: bool) -> None:
        entry = cmt_entry(address=address, dirty=dirty)
        self.cache[lpa] = entry
        self.lru_list.insert(0, lpa)
        if len(self.cache) >= CMT_SIZE:
            lru_lpa, leaving_entry = self.eject_entry()
            debug_info(f"[CMT] <add_entry> ejecting entry: ({lru_lpa}, {repr(leaving_entry[1])})")
            self.address_mapping_unit.generate_mapping_write_transaction(self.cache, lru_lpa/LPA_NO_PER_MAPPING_PAGE)


from dataclasses import dataclass

@dataclass
class blockBKE:
    invalid_pages: int   # 存储无效页的 page_id
    write_frontier: int  # 下次写入的目标page_id
    wl_level: int        # 该block被erase的次数
    def __init__(self, invalid_pages: int, write_frontier: int, wl_level: int) -> None:
        self.invalid_pages = invalid_pages
        self.write_frontier = write_frontier
        self.wl_level = wl_level

@dataclass
class PlaneBKE:
    num_free_pages: int
    block_entries: list[blockBKE] = field(default_factory=list)
    write_frontier_block: int
    def __init__(self) -> None:
        self.num_free_pages = PAGE_PER_BLOCK * BLOCK_PER_PLANE
        self.block_entries = [blockBKE(invalid_pages=0, write_frontier=0, wl_level=0) for _ in range(BLOCK_PER_PLANE)]
        self.write_frontier_block = 0



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
        plane_bke.num_free_pages -= 1
        bke = plane_bke.block_entries[plane_bke.write_frontier_block]
        page_id = bke.write_frontier
        bke.write_frontier += 1
        if bke.write_frontier == PAGE_PER_BLOCK:
            bke.write_frontier = 0
            plane_bke.write_frontier_block += 1
        return FlashAddress(channel=channel_id, chip=chip_id, die=die_id, plane=plane_id, sub_plane=plane_bke.write_frontier_block, page=page_id)
    
    def _set_barrier(self, tr: Transaction):
        debug_info(f"[Block Manager] set_barrier: tr: {repr(tr)}")
        self.lpa_protected_book[tr.lpa] = tr

    def _remove_barrier(self, tr: Transaction):
        if tr.type in [TransactionType.USER_WRITE, TransactionType.MAPPING_WRITE, TransactionType.GC_WRITE, TransactionType.GC_ERASE]: # only these transactions need to remove barrier
            debug_info(f"[Block Manager] <_remove_barrier> tr: {repr(tr)}")
            self.lpa_protected_book.pop(tr.lpa)

    def get_block_bke(self, addr: FlashAddress) -> blockBKE:
        # addr: 6 元组 (channel, chip, die, plane, block, page)，使用前 5 维索引
        channel_id, chip_id, die_id, plane_id, block_id = addr.channel, addr.chip, addr.die, addr.plane, addr.sub_plane
        return self.block_keeping_book[channel_id][chip_id][die_id][plane_id][block_id]

    def is_free(self, addr: FlashAddress) -> bool:
        bke = self.get_block_bke(addr)
        return len(bke.valid_pages) == 0 and len(bke.invalid_pages) == 0 and len(bke.free_pages) != 0

    def is_protected(self, addr: FlashAddress) -> bool:
        # 这里按需求增加是否保护的判断，可添加bke的protected属性
        bke = self.block_keeping_book[addr.channel][addr.chip][addr.die][addr.plane][addr.sub_plane]
        return bke.page_protected[addr.page]

    def mark_used(self, addr: FlashAddress, page_id: int):
        bke = self.get_block_bke(addr)
        # 将 page_id 从 free_pages 移动到 valid_pages，更新写前沿
        if page_id in bke.free_pages:
            bke.free_pages.remove(page_id)
        bke.valid_pages.add(page_id)
        bke.write_frontier = (page_id + 1) % self.pages_per_block

    def mark_invalid(self, addr: FlashAddress, page_id: int):
        bke = self.get_block_bke(addr)
        # 将 page_id 从 valid_pages 移动到 invalid_pages
        if page_id in bke.valid_pages:
            bke.valid_pages.remove(page_id)
        bke.invalid_pages.add(page_id)

    def erase_block(self, addr: FlashAddress):
        bke = self.get_block_bke(addr)
        # 清空所有有效、无效页，将页全设为free，写前沿归零，擦除次数+1
        bke.free_pages = set(range(self.pages_per_block))
        bke.valid_pages.clear()
        bke.invalid_pages.clear()
        bke.write_frontier = 0
        bke.wl_level += 1
        bke.protected = False


class GC_WL_Manager:
    def __init__(self):
        print("Initializing GC/WL Manager...")
        pass
        print("GC/WL Manager initialization complete.")

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
        self.barriered_trans = []
        print("TSU initialization complete.")
    
    def _removing_barrier(self, tr: Transaction):
        self.Prepare_trans_submission()                 
        # removing barrier set by tr
        if tr.type in [TransactionType.USER_WRITE, TransactionType.MAPPING_WRITE, TransactionType.GC_WRITE, TransactionType.GC_ERASE]:
            print(f"[TSU] <_removing_barrier> following tr is removing barrier: {repr(tr)}")
            # 按 address 收集要提交的 transaction，再按 id 移除，避免 list.remove(tr) 触发
            # Transaction.__eq__ 递归比较 rely_on_transactions/required_by_transactions 导致栈溢出
            to_submit = [trans for trans in self.barriered_trans if trans.lpa == tr.lpa]
            for trans in to_submit:
                self.Submit_trans(trans)
            ids_to_remove = {id(trans) for trans in to_submit}
            self.barriered_trans = [trans for trans in self.barriered_trans if id(trans) not in ids_to_remove]
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
        print(f"[TSU] <Prepare_trans_submission> {self._onfly_schedule_req_no}")

    def Submit_trans(self, trans):
        """Endeque a transaction to the appropriate per-chip priority deque."""
        if trans.lpa in self.block_manager.lpa_protected_book.keys() and self.block_manager.lpa_protected_book[trans.lpa] != trans:
            debug_info(f"[TSU] <Submit_trans> facing barrier, lpa: {trans.lpa}, trans: {repr(trans)}")
            self.barriered_trans.append(trans)
            return
        debug_info(f"[TSU] <Submit_trans> submitting trans: {repr(trans)}")
        channel = trans.address.channel
        chip    = trans.address.chip
        self.queues[channel][chip][trans.type].append(trans)

    def Schedule(self):
        """Close batch and, if all batches are closed, drive scheduling.
        对标 TSU_OutOfOrder::Schedule()。
        """
        self._onfly_schedule_req_no -= 1
        print(f"[TSU] <Schedule> {self._onfly_schedule_req_no}")
        if self._onfly_schedule_req_no < 0:
            raise RuntimeError("onfly_schedule_req_no should not be negative")
        if self._onfly_schedule_req_no > 0:
            return
        for ch in range(self.channel_no):
            if self.channel_is_busy(ch):
                print(f"[TSU] <Schedule> channel {ch} is busy, move to next channel")
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
            print(f"[TSU] <try_activate> SEARCH/COMPUTE-dedicated chip {chip_id}")
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
        self.issue_command(chip_id, q1, q2, suspension_required)
        return True

    def try_write(self, chip_id) -> bool:
        """Try to issue a write command to the chip.

        对标 TSU_OutOfOrder::service_write_transaction()。
        Allows Erase Suspension for writes; disallows Write-on-Write suspension.
        """
        chip_bke = self.phy.get_chip_bke(chip_id)
        chip_status = chip_bke.status
        suspension_required = False

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
        self.issue_command(chip_id, q1, q2, suspension_required)
        return True

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
        self.issue_command(chip_id, q, None, False)
        return True

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
        self.issue_compute_command(chip_id, q)
        return True

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
        self.issue_search_command(chip_id, q)
        return True

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
        self.issue_static_write_command(chip_id, q)
        return True

    # ── Command dispatch to PHY ───────────────────────────────────────────────

    def issue_command(
        self,
        chip_id,
        q1: list,
        q2,
        suspension_required: bool,
    ) -> None:
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
        print(f"[TSU] <issue_command> q1: {q1}, q2: {q2}, suspension_required: {suspension_required}")
        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q1[0].address.die
        start_page = q1[0].address.page

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            page_id = start_page if _step == 0 else None
            plane_vector = 0
            dispatch_slots: list = []

            for tr in list(q1):
                if tr.rely_on_transactions:
                    print(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready and ("read" not in tr.type.value.lower()):
                    print(f"[TSU] <issue_command> data not ready, skipping {repr(tr)}")
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
                        print(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                        continue
                    if not tr.data_ready and ("read" not in tr.type.value.lower()):
                        print(f"[TSU] <issue_command> data not ready, skipping {repr(tr)}")
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
                print(f"[TSU] <issue_command> dispatching {len(dispatch_slots)} transactions to PHY")
                for tr in dispatch_slots:
                    if tr in q1:
                        q1.remove(tr)
                    elif q2 is not None and tr in q2:
                        q2.remove(tr)
                self.phy.send_command_to_chip(chip_id, dispatch_slots, suspension_required)
                dispatch_slots = []
        return

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

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_vector = 0
            dispatch_slots: list = []

            for tr in list(q):
                if tr.rely_on_transactions:
                    debug_info(f"[TSU] <issue_search_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready:
                    debug_info(f"[TSU] <issue_search_command> data not ready, skipping {repr(tr)}")
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

    def issue_compute_command(self, chip_id, q: list):
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
                    debug_info(f"[TSU] <issue_compute_command> data not ready, skipping {repr(tr)}")
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

    def issue_static_write_command(self, chip_id, q: list):
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
                    debug_info(f"[TSU] <issue_static_write_command> data not ready, skipping {repr(tr)}")
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
        return
    # ── Helpers ───────────────────────────────────────────────────────────────

    def channel_is_busy(self, channel_id: int) -> bool:
        return self.phy.channel_is_busy(channel_id)

    def is_static_chip(self, chip_id) -> bool:
        """Returns True if chip is dedicated to SEARCH/COMPUTE. Must match get_static_address() which maps static sub_planes to chip 0..STATIC_CHIP_PER_CHANNEL-1."""
        return chip_id[1] >= CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL


class Address_Mapping_Domain:
    def __init__(self):
        self.cmt: CMT
        self._construction_valid: bool = False
    
    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating Address Mapping Domain construction...")
        assert self.cmt is not None, "Address Mapping Domain cmt is not set"
        assert self.gmt is not None, "Address Mapping Domain gmt is not set"
        assert self.gtd is not None, "Address Mapping Domain gtd is not set"
        assert self.tsu is not None, "Address Mapping Domain tsu is not set"
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
            assert domain.gtd == self.gtd, "Address Mapping Unit domains gtd is not the same as Address Mapping Unit gtd"
            domain.Validate_construction()
        print("Address Mapping Unit construction validation complete.")

    def __init__(self):
        print("Initializing Address Mapping Unit...")
        self._construction_valid: bool = False
        self.domains = [Address_Mapping_Domain() for _ in range(NUM_OF_QUEUES)]
        self.waiting_for_mapping_trans: dict[int, list[Transaction]] = defaultdict(list)
        self.tsu: TSU
        self.cmt: CMT
        self.gmt: dict[int, cmt_entry] = {}
        self.gtd: dict[int, GTDEntry] = {}
        self.block_manager: Block_Manager
        for domain in self.domains:
            domain.gtd = self.gtd
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
    
    def _handle_mapping_read_response(self, tr: Transaction):
        # handle response for tr waiting mapping info
        if tr.type == TransactionType.MAPPING_READ:
            print(f"[AMU] <_handle_mapping_read_response> response tr: {repr(tr)}")
            self.tsu.Prepare_trans_submission()
            # get arriving lpa in the finished mapping read transaction
            arriving_lpa = []
            for i in range(len(tr.bitmap)):
                if tr.bitmap[i] == 0:
                    continue
                lpa = i + tr.lpa * LPA_NO_PER_MAPPING_PAGE
                arriving_lpa.append(lpa) 
            debug_info(f"[AMU] <_handle_mapping_read_response> arriving_lpa: {arriving_lpa}, response: {tr.response}")
            # check if the arriving lpa number matches the response number
            if len(arriving_lpa) != len(tr.response):
                raise ValueError(f"Arriving lpa number mismatch, arriving_lpa: {arriving_lpa}, response: {tr.response}")
            # submit the waiting transactions for the arriving lpa, and update the cmt meanwhile
            for i in range(len(arriving_lpa)):
                lpa = arriving_lpa[i]
                ppa = tr.response[i]
                # add entry in cmt
                domain = self.domains[tr.source_req.sq_id]
                if not domain.cmt.is_cached(lpa):
                    domain.cmt.add_entry(lpa, self.translate_ppa_to_address(ppa), dirty=False)
                waiting_trs = self.waiting_for_mapping_trans[lpa]
                debug_info(f"[AMU] <_handle_mapping_read_response> number of waiting trs: {len(waiting_trs)}")
                for waiting_tr in waiting_trs:
                    address = self.translate_ppa_to_address(ppa)
                    waiting_tr.address = address
                    domain = self.domains[waiting_tr.source_req.sq_id]
                    domain.cmt.write(lpa, address, dirty=False)
                    self.tsu.Submit_trans(waiting_tr)
                self.waiting_for_mapping_trans[lpa].clear()
            self.tsu.Schedule()
        debug_info(f"[AMU] <_handle_mapping_read_response> done")
        return
    
    def translate_and_submit(self, req: Request):
        # SEARCH and COMPUTE requests don't need to be translated
        print(f"[AMU] translate_and_submit: handling new request: {repr(req)}")
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
                    if entry.valid_lpa_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] == 0:
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
                self.block_manager._set_barrier(tr)
                domain = self.domains[req.sq_id]
                domain.cmt.add_entry(tr.lpa, page_address, dirty=True) # dirty is true because a write tr must update the ppa of a lpa
                debug_info(f"[AMU] <translate_and_submit> add new entry to cmt: {tr.lpa}, {page_address}")
                self.tsu.Submit_trans(tr)
        # process search and compute requests, whose transaction ppa is decided in segment step
        elif req.type == RequestType.SEARCH:
            assert req.transaction_list is not None, "Search request transaction list is not set"
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
        elif req.type == RequestType.COMPUTE:
            assert req.transaction_list is not None, "Compute request transaction list is not set"
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
        else:
            raise ValueError("Invalid request type for translate_and_submit")
        print("[AMU] <translate_and_submit> Prepare trans submission complete")
        self.tsu.Schedule()
        print("[AMU] <translate_and_submit> TSU Schedule complete")
        return
    
    def generate_mapping_write_transaction(self, cache: dict[int, cmt_entry], mvpn: int) -> None:
        self.tsu.Prepare_trans_submission()
        bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        data = [None for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        for lpa, entry in cache.items():
            if lpa // LPA_NO_PER_MAPPING_PAGE != mvpn: # write back clear entry in the meantime
                continue
            index = lpa % LPA_NO_PER_MAPPING_PAGE
            bitmap[index] = 1
            data[index] = self.translate_address_to_ppa(entry.address)
            self.gmt[lpa] = entry
            self.cmt.eject_entry(lpa)
        if mvpn not in self.gtd:
            # writing to a new mapping page, get page address for it
            page_address = self.get_plane_address_for_mvpn(mvpn)
            page_address = self.block_manager.get_write_frontier(page_address)
            self.gtd[mvpn] = GTDEntry(address=page_address, valid_lpa_bitmap=bitmap)
            write_tr = Transaction(
                source_req=None,
                type=TransactionType.MAPPING_WRITE,
                mvpn=mvpn,
                address=page_address,
                payload=data,
                bitmap=bitmap
            )
            self.tsu.Submit_trans(write_tr)
        else:
            gtd_entry = self.gtd[mvpn]
            write_tr = Transaction(
                source_req=None,
                type=TransactionType.MAPPING_WRITE,
                mvpn=mvpn,
                address=gtd_entry.address,
                payload=data,
                bitmap=bitmap
            )
            need_read = False
            for i in range(LPA_NO_PER_MAPPING_PAGE):
                if gtd_entry.valid_lpa_bitmap[i] == 1 and bitmap[i] == 0:
                    need_read = True
                    break
            # working
            if need_read:
                read_tr = self.generate_mapping_read_transaction(write_tr, mvpn)
                read_tr.required_by_transactions.append(write_tr)
                write_tr.rely_on_transactions.append(read_tr)
                self.tsu.Submit_trans(read_tr)

            self.tsu.Submit_trans(write_tr)
        self.tsu.Schedule()
        return
    def generate_mapping_read_transaction(self, trigger_tr: Transaction, mvpn) -> Transaction:
        mapping_page_address = self.gtd[mvpn].address
        access_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        access_bitmap[trigger_tr.lpa % LPA_NO_PER_MAPPING_PAGE] = 1
        read_tr = Transaction(
            source_req=trigger_tr.source_req,
            type=TransactionType.MAPPING_READ,
            mvpn=mvpn,
            address=mapping_page_address,
            data_ready=True,
            bitmap=access_bitmap
        )
        return read_tr


    def get_plane_address_for_mvpn(self, mvpn) -> FlashAddress:
        """
        choose a plane address to store the new GTDEntry for mvpn.
        here we simply tread mvpn as a lpa and use the static address mapping scheme to get a plane address, which is not optimized but simple.
        """
        return self.get_plane_address_for_lpa(mvpn)

    def get_plane_address_for_lpa(self, lpa) -> FlashAddress:
        # LPA 从低到高: page in block, block in plane, plane in die, die, chip, channel.
        # 先除以 (block*page) 再对 PLANE_PER_DIE 取模，得到 plane 索引 [0, PLANE_PER_DIE-1]
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

    def translate_address_to_ppa(self, address: FlashAddress) -> int:
        channel_id = address.channel
        chip_id = address.chip
        die_id = address.die
        plane_id = address.plane
        sub_plane_id = address.sub_plane
        page_id = address.page
        return (((((channel_id * CHIP_PER_CHANNEL + chip_id) * DIE_PER_CHIP + die_id) * PLANE_PER_DIE + plane_id) * BLOCK_PER_PLANE + sub_plane_id) * PAGE_PER_BLOCK + page_id)


    def translate_ppa_to_address(self, ppa: int) -> FlashAddress:
        page_id = ppa % PAGE_PER_BLOCK
        ppa //= PAGE_PER_BLOCK
        sub_plane_id = ppa % BLOCK_PER_PLANE
        ppa //= BLOCK_PER_PLANE
        plane_id = ppa % PLANE_PER_DIE
        ppa //= PLANE_PER_DIE
        die_id = ppa % DIE_PER_CHIP
        ppa //= DIE_PER_CHIP
        chip_id = ppa % CHIP_PER_CHANNEL
        channel_id = ppa // CHIP_PER_CHANNEL
        return FlashAddress(channel=channel_id, chip=chip_id, die=die_id, plane=plane_id, sub_plane=sub_plane_id, page=page_id)


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
