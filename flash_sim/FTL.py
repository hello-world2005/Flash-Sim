# -*- coding: utf-8 -*-
from dataclasses import dataclass
from nt import access
from typing import Mapping
from pathlib import Path
import json

from .common import *
# #region agent log
def _dbg_log(data: dict):
    import time
    import os
    bases = [
        Path(__file__).resolve().parent.parent,  # 项目根
        Path(__file__).resolve().parent,         # flash_sim/
        Path(os.getcwd()),                       # 当前工作目录兜底
    ]
    for base in bases:
        try:
            p = base / "debug-6da53a.log"
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({**data, "ts": time.time()}, ensure_ascii=False) + "\n")
            return
        except Exception:
            continue
# #endregion
from .PHY import PHY
from . import utils
from collections import deque as deque

class CMT:
    def __init__(self):
        self.cache: dict[int, cmt_entry] = {}
        self.lru_list: list[int] = []

    def query(self, lpa: int) -> cmt_entry | None:
        if lpa in self.cache:
            self.lru_list.remove(lpa)
            self.lru_list.insert(0, lpa)
            return self.cache[lpa]
        return None

    def write(self, lpa: int, ppa: FlashAddress, dirty: bool = True):
        entry = cmt_entry(ppa=ppa, dirty=dirty)
        if lpa in self.cache:
            self.cache[lpa] = entry
            self.lru_list.remove(lpa)
            self.lru_list.insert(0, lpa)
        else:
            if len(self.cache) >= CMT_SIZE:
                lru_lpa = self.lru_list.pop()
                del self.cache[lru_lpa]
            self.cache[lpa] = entry
            self.lru_list.insert(0, lpa)


from dataclasses import dataclass

@dataclass
class blockBKE:
    free_pages: deque      # 存储空闲页的 page_id
    valid_pages: deque     # 存储有效页的 page_id
    invalid_pages: deque   # 存储无效页的 page_id
    write_frontier: int  # 下次写入的目标page_id
    wl_level: int        # 该block被erase的次数
    page_protected: list[bool] = field(default_factory=list)     # 该block当前是否上锁，防止读写竞争
    def __init__(self, free_pages: deque, valid_pages: deque, invalid_pages: deque, write_frontier: int, wl_level: int) -> None:
        self.free_pages = free_pages
        self.valid_pages = valid_pages
        self.invalid_pages = invalid_pages
        self.write_frontier = write_frontier
        self.wl_level = wl_level
        self.page_protected = []
        for _ in range(PAGE_PER_BLOCK):
            self.page_protected.append(False)

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
        print("Initializing block keeping book...")
        # 结构为：channel -> chip -> die -> plane -> [blockBKE, ...]，与 address 前 5 维一致
        self.block_keeping_book = [
            [
                [
                    [
                        [
                            blockBKE(
                                free_pages=deque(list(range(pages_per_block))),
                                valid_pages=deque(),
                                invalid_pages=deque(),
                                write_frontier=0,
                                wl_level=0
                            ) for _ in range(block_no_per_plane)
                        ]
                        for _ in range(plane_no_per_die)
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
        block_keeping_books = self.block_keeping_book[channel_id][chip_id][die_id][plane_id]
        max_free_pages = -1
        chosen_block_id = -1
        for i in range(self.block_no_per_plane):
            bke = block_keeping_books[i]
            if len(bke.free_pages) > max_free_pages:
                max_free_pages = len(bke.free_pages)
                chosen_block_id = i
        if chosen_block_id == -1:
            raise ValueError(f"No free block found in plane {plane_address}")
        bke = block_keeping_books[chosen_block_id]
        page_id = bke.write_frontier
        bke.free_pages.remove(page_id)
        bke.valid_pages.append(page_id)
        bke.write_frontier = bke.free_pages.popleft()
        return FlashAddress(channel=channel_id, chip=chip_id, die=die_id, plane=plane_id, sub_plane=chosen_block_id, page=page_id)
    
    def set_barrier(self, tr: Transaction):
        bke = self.get_block_bke(tr.address)
        bke.page_protected[tr.address.page] = True

    def _remove_barrier(self, tr: Transaction):
        if "write" in tr.type.value.lower() or "erase" in tr.type.value.lower(): # only write and erase need to remove barrier
            print(f"[Block Manager] _remove_barrier: tr: {repr(tr)}")
            bke = self.get_block_bke(tr.address)
            bke.page_protected[tr.address.page] = False

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
    
    def _submit_barriered_trans(self, tr_removing_barrier: Transaction):
        if tr_removing_barrier.type not in [TransactionType.USER_WRITE, TransactionType.MAPPING_WRITE, TransactionType.GC_WRITE, TransactionType.GC_ERASE]:
            return
        print(f"[TSU] _submit_barriered_trans: following tr is removing barrier: {repr(tr_removing_barrier)}")
        # 按 address 收集要提交的 transaction，再按 id 移除，避免 list.remove(tr) 触发
        # Transaction.__eq__ 递归比较 rely_on_transactions/required_by_transactions 导致栈溢出
        to_submit = [tr for tr in self.barriered_trans if tr.lpa == tr_removing_barrier.lpa]
        for tr in to_submit:
            self.Submit_trans(tr)
            print(f"[TSU] _submit_barriered_trans: submitted tr: {repr(tr)}")
        ids_to_remove = {id(tr) for tr in to_submit}
        self.barriered_trans = [t for t in self.barriered_trans if id(t) not in ids_to_remove]

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
        print(f"Prepare_trans_submission: {self._onfly_schedule_req_no}")

    def Submit_trans(self, trans):
        """Endeque a transaction to the appropriate per-chip priority deque."""
        if self.block_manager.is_protected(trans.address):
            print(f"Submit_trans: facing barrier, address: {repr(trans.address)}, trans: {repr(trans)}")
            self.barriered_trans.append(trans)
            return
        channel = trans.address.channel
        chip    = trans.address.chip
        self.queues[channel][chip][trans.type].append(trans)

    def Schedule(self):
        """Close batch and, if all batches are closed, drive scheduling.

        对标 TSU_OutOfOrder::Schedule()。
        """
        onfly_before = self._onfly_schedule_req_no
        self._onfly_schedule_req_no -= 1
        print(f"Schedule: {self._onfly_schedule_req_no}")
        if self._onfly_schedule_req_no < 0:
            raise RuntimeError("onfly_schedule_req_no should not be negative")
        if self._onfly_schedule_req_no > 0:
            return
        for ch in range(self.channel_no):
            if self.channel_is_busy(ch):
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
        for chip_no in range(CHIP_PER_CHANNEL):
            chip_id_check = (channel_id, chip_no)
            if chip_id_check not in self.phy._chip_bkes:
                raise ValueError(f"Chip {chip_id_check} not found in PHY while broadcasting channel idle")
            bke = self.phy._chip_bkes[chip_id_check]
            if not bke._waiting_data_out:
                continue
            self.phy._broadcast_channel_idle(channel_id)
            return
        for _ in range(self.chip_no_per_channel):
            chip_id = (channel_id, self.round_robin_turn[channel_id])
            self.try_activate(chip_id)
            self.round_robin_turn[channel_id] = (
                (self.round_robin_turn[channel_id] + 1) % self.chip_no_per_channel
            )
            if self.channel_is_busy(channel_id):
                break

    def _on_chip_idle(self, chip_id):
        """对标 handle_chip_idle_signal(): chip 空闲且 channel 空闲时尝试激活。"""
        channel_id = chip_id[0]
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
            if self.try_compute(chip_id):
                print(f"try_activate: compute dispatched for chip {chip_id}")
                dispatched = True
            if not dispatched and self.try_search(chip_id):
                print(f"try_activate: search dispatched for chip {chip_id}")
                dispatched = True
            return dispatched
        if self.try_read(chip_id):
            print(f"try_activate: read dispatched for chip {chip_id}")
            dispatched = True
        elif self.try_write(chip_id):
            print(f"try_activate: write dispatched for chip {chip_id}")
            dispatched = True
        elif self.try_erase(chip_id):
            print(f"try_activate: erase dispatched for chip {chip_id}")
            dispatched = True
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

        q = self.queues[chip_id[0]][chip_id[1]].get("user_compute")
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

        q = self.queues[chip_id[0]][chip_id[1]].get("user_search")
        if not q:
            return False
        self.issue_search_command(chip_id, q)
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
                if tr.address.die != die_id:
                    continue
                if tr.rely_on_transactions:
                    print(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                    continue
                if not tr.data_ready and (tr.type.value.lower() != "read"):
                    print(f"[TSU] <issue_command> data not ready, skipping {repr(tr)}")
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
                    if tr.address.die != die_id:
                        continue
                    if tr.rely_on_transactions:
                        print(f"[TSU] <issue_command> tr has rely_on_transactions, skipping {repr(tr)}")
                        continue
                    if not tr.data_ready and (tr.type.value.lower() != "read"):
                        print(f"[TSU] <issue_command> data not ready, skipping {repr(tr)}")
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
        start_sub_plane = q[0].address.sub_plane

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_vector = 0
            dispatch_slots: list = []

            for tr in list(q):
                if tr.address.die != die_id:
                    continue
                tr_plane = tr.address.plane
                if plane_vector & (1 << tr_plane):
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def channel_is_busy(self, channel_id: int) -> bool:
        return self.phy.channel_is_busy(channel_id)

    def is_static_chip(self, chip_id) -> bool:
        """Returns True if chip is dedicated to SEARCH/COMPUTE. Must match get_static_address() which maps static sub_planes to chip 0..STATIC_CHIP_PER_CHANNEL-1."""
        return chip_id[1] >= CHIP_PER_CHANNEL - STATIC_CHIP_PER_CHANNEL


class Address_Mapping_Domain:
    def __init__(self):
        self.cmt = CMT()
        self.gmt: dict[int, cmt_entry] = {}
        self.DepartingEntry = []
        self.ArrivingEntry = []
        self.tsu : TSU
        self.gtd: dict[int, GTDEntry]
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
        entry = self.cmt.query(transaction.lpa)
        if entry is not None:
            transaction.address = entry.ppa
            return True
        if transaction.lpa in self.gmt:
            transaction.address = self.gmt[transaction.lpa].ppa
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
        self.waiting_for_mapping_trans: list = []
        self.tsu: TSU
        self.gtd: dict[int, GTDEntry] = {}
        self.block_manager: Block_Manager
        for domain in self.domains:
            domain.gtd = self.gtd
        
    def translate_and_submit(self, req: Request):
        # SEARCH and COMPUTE requests don't need to be translated
        print(f"[AMU] translate_and_submit: handling new request: {repr(req)}")
        if req.type in (RequestType.SEARCH, RequestType.COMPUTE):
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
                    print("Cache hit")
                    self.tsu.Submit_trans(tr)
                else:
                    print("Cache miss")
                    self.waiting_for_mapping_trans.append(tr)
                    mvpn = tr.lpa // LPA_NO_PER_MAPPING_PAGE
                    if mvpn not in self.gtd:
                        raise ValueError("Read request accessing non-existing mapping page")
                    entry = self.gtd[mvpn]
                    if entry.valid_lpa_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] == 0:
                        raise ValueError("Read request accessing invalid lpa in mapping page")
                    print("Read mapping page")
                    read_tr = self.generate_mapping_read_transaction(tr, mvpn)
                    tr.rely_on_transactions.append(read_tr)
                    read_tr.required_by_transactions.append(tr)
                    self.tsu.Submit_trans(tr)
                    self.tsu.Submit_trans(read_tr)
        elif req.type == RequestType.WRITE:
            for tr in req.transaction_list:
                if domain.query_cmt(tr):
                    print("Cache hit")
                    self.tsu.Submit_trans(tr)
                else:
                    print("Cache miss")
                    self.waiting_for_mapping_trans.append(tr)
                    mvpn = tr.lpa // LPA_NO_PER_MAPPING_PAGE
                    lpa_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
                    lpa_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] = 1
                    if mvpn not in self.gtd:
                        print("New mapping page")
                        write_mapping_info_tr = self.generate_mapping_write_transaction(tr, mvpn, lpa_bitmap, new_page=True)
                        # add an entry in gtd
                        entry = GTDEntry(address=write_mapping_info_tr.address, valid_lpa_bitmap=lpa_bitmap)
                        entry.set_valid_lpa_bitmap(tr.lpa, 1) # set the valid lpa bitmap of the lpa to 1
                        self.gtd[mvpn] = entry
                        # get a page for user write request
                        plane_address = self.get_plane_address_for_lpa(tr.lpa)
                        page_address = self.block_manager.get_write_frontier(plane_address)
                        tr.address = page_address
                        # add an entry in cmt
                        domain.cmt.write(tr.lpa, page_address, dirty=False)
                        # set relationship
                        tr.rely_on_transactions.append(write_mapping_info_tr)
                        write_mapping_info_tr.required_by_transactions.append(tr)
                        self.tsu.Submit_trans(tr)
                        self.block_manager.set_barrier(tr)                                # barrier should be set once a write transaction is issued to prevent from a read access before this write transaction is executed
                        self.tsu.Submit_trans(write_mapping_info_tr)
                        self.block_manager.set_barrier(write_mapping_info_tr)
                    else:
                        entry = self.gtd[mvpn]
                        if entry.valid_lpa_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] == 0: 
                            print("Update mapping page")
                            # still need to write a new lpa-ppa mapping info in entry
                            write_mapping_info_tr = self.generate_mapping_write_transaction(tr, mvpn, lpa_bitmap, new_page=False)
                            tr.rely_on_transactions.append(write_mapping_info_tr)
                            write_mapping_info_tr.required_by_transactions.append(tr)
                            self.tsu.Submit_trans(tr)
                            self.block_manager.set_barrier(tr)
                            self.tsu.Submit_trans(write_mapping_info_tr)
                            self.block_manager.set_barrier(write_mapping_info_tr)
                        else:
                            print("Read mapping page")
                            # we only need to read the mapping page
                            read_tr = self.generate_mapping_read_transaction(tr, mvpn)
                            read_tr.required_by_transactions.append(tr)
                            tr.rely_on_transactions.append(read_tr)
                            self.tsu.Submit_trans(tr)
                            self.tsu.Submit_trans(read_tr)
        else:
            raise ValueError("Invalid request type for translate_and_submit")
        print("[AMU] <translate_and_submit> Prepare trans submission complete")
        self.tsu.Schedule()
        print("[AMU] <translate_and_submit> TSU Schedule complete")
        return
    
    def generate_mapping_write_transaction(self, trigger_tr: Transaction, mvpn, lpa_bitmap: list[int], new_page = True) -> Transaction:
        if new_page: # the mvpn has no mapping page for it, we need to find a new page to store this mapping page
            mapping_plane_address = self.get_plane_address_for_mvpn(mvpn) # choose a plane to store the mapping page of mvpn
            mapping_page_address = self.block_manager.get_write_frontier(mapping_plane_address)
            sector_bitmap = self.translate_lpa_bitmap_to_sector_bitmap(lpa_bitmap)
            write_mapping_info_tr = Transaction(
                source_req=trigger_tr.source_req,
                type=TransactionType.MAPPING_WRITE,
                lpa=mvpn,
                address=mapping_page_address,
                bitmap=sector_bitmap,
                data_ready=True
            )
            return write_mapping_info_tr
        else: # the page already exists for mvpn, we only need to update it if necessary
            mapping_page_address = self.gtd[mvpn].address
            valid_lpa_bitmap = self.gtd[mvpn].valid_lpa_bitmap
            write_mapping_info_tr = Transaction(
                source_req=trigger_tr.source_req,
                type=TransactionType.MAPPING_WRITE,
                lpa=mvpn,
                address=mapping_page_address,
                data_ready=True
            )
            new_lpa_bitmap = [valid_lpa_bitmap[i] or lpa_bitmap[i] for i in range(LPA_NO_PER_MAPPING_PAGE)]
            write_mapping_info_tr.bitmap = self.translate_lpa_bitmap_to_sector_bitmap(new_lpa_bitmap)
            self.gtd[mvpn].valid_lpa_bitmap = new_lpa_bitmap # update the valid lpa bitmap of the mapping page
            if lpa_bitmap != new_lpa_bitmap: # we need to read the mapping page to get mapping info of other lpa
                read_tr = self.generate_mapping_read_transaction(trigger_tr, mvpn)
                write_mapping_info_tr.rely_on_transactions.append(read_tr)
                read_tr.required_by_transactions.append(write_mapping_info_tr)
                self.tsu.Submit_trans(read_tr)
            return write_mapping_info_tr
    
    def translate_lpa_bitmap_to_sector_bitmap(self, lpa_bitmap: list[int]) -> list[int]:
        sector_bitmap = [0 for _ in range(SECTOR_PER_PAGE)]
        for i in range(LPA_NO_PER_MAPPING_PAGE):
            if lpa_bitmap[i] == 1:
                sector_bitmap[i // LPA_NO_PER_SECTOR] = 1
        return sector_bitmap

    def generate_mapping_read_transaction(self, trigger_tr: Transaction, mvpn) -> Transaction:
        mapping_page_address = self.gtd[mvpn].address
        access_bitmap = [0 for _ in range(LPA_NO_PER_MAPPING_PAGE)]
        access_bitmap[trigger_tr.lpa % LPA_NO_PER_MAPPING_PAGE // SECTOR_PER_PAGE] = 1
        access_bitmap = self.translate_lpa_bitmap_to_sector_bitmap(access_bitmap)
        read_tr = Transaction(
            source_req=trigger_tr.source_req,
            type=TransactionType.MAPPING_READ,
            lpa=mvpn,
            address=mapping_page_address,
            data_ready=True,
            bitmap=access_bitmap
        )
        return read_tr


    def handle_mapping_read_response(self, response):
        # 用 response 更新 CMT/GMT，并继续处理 waiting_read_write_trans / waiting_search_compute_req
        pass

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
