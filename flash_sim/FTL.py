# -*- coding: utf-8 -*-
from dataclasses import dataclass
from nt import access
from typing import Mapping

from common import *
from PHY import PHY
import utils

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

    def write(self, lpa: int, ppa: int, dirty: bool = True):
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
    free_pages: set      # 存储空闲页的 page_id
    valid_pages: set     # 存储有效页的 page_id
    invalid_pages: set   # 存储无效页的 page_id
    write_frontier: int  # 下次写入的目标page_id
    wl_level: int        # 该block被erase的次数
    protected: bool     # 该block当前是否上锁，防止读写竞争

class Block_Manager:
    def __init__(self,
                 channel_no=CHANNEL_NO,
                 chip_no_per_channel=CHIP_PER_CHANNEL,
                 die_no_per_chip=DIE_PER_CHIP,
                 plane_no_per_die=PLANE_PER_DIE,
                 block_no_per_plane=BLOCK_PER_PLANE,
                 pages_per_block=PAGE_PER_BLOCK):
        self.channel_no = channel_no
        self.chip_no_per_channel = chip_no_per_channel
        self.die_no_per_chip = die_no_per_chip
        self.plane_no_per_die = plane_no_per_die
        self.block_no_per_plane = block_no_per_plane
        self.pages_per_block = pages_per_block

        # 结构为：channel -> chip -> die -> plane -> [blockBKE, ...]，与 address 前 5 维一致
        self.block_keeping_book = [
            [
                [
                    [
                        [
                            blockBKE(
                                free_pages=set(range(pages_per_block)),
                                valid_pages=set(),
                                invalid_pages=set(),
                                write_frontier=0,
                                wl_level=0,
                                protected=False,
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

    def get_block_bke(self, addr):
        # addr: 6 元组 (channel, chip, die, plane, block, page)，使用前 5 维索引
        channel_id, chip_id, die_id, plane_id, block_id = addr[0], addr[1], addr[2], addr[3], addr[4]
        return self.block_keeping_book[channel_id][chip_id][die_id][plane_id][block_id]

    def is_free(self, addr) -> bool:
        bke = self.get_block_bke(addr)
        return len(bke.valid_pages) == 0 and len(bke.invalid_pages) == 0 and len(bke.free_pages) != 0

    def is_not_protected(self, addr) -> bool:
        # 这里按需求增加是否保护的判断，可添加bke的protected属性
        bke = self.get_block_bke(addr)
        return not getattr(bke, "protected", False)

    def mark_used(self, addr, page_id):
        bke = self.get_block_bke(addr)
        # 将 page_id 从 free_pages 移动到 valid_pages，更新写前沿
        if page_id in bke.free_pages:
            bke.free_pages.remove(page_id)
        bke.valid_pages.add(page_id)
        bke.write_frontier = (page_id + 1) % self.pages_per_block

    def mark_invalid(self, addr, page_id):
        bke = self.get_block_bke(addr)
        # 将 page_id 从 valid_pages 移动到 invalid_pages
        if page_id in bke.valid_pages:
            bke.valid_pages.remove(page_id)
        bke.invalid_pages.add(page_id)

    def erase_block(self, addr):
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
        pass


def _get_lpa_sector_in_mapping_page(lpa: int) -> int:
    return lpa % LPA_NO_PER_MAPPING_PAGE


class TSU:
    """Transaction Scheduling Unit — Out-of-Order version.

    对标 MQSim TSU_OutOfOrder。管理 9 级调度队列，以 channel 为单位轮询 chip，
    按 读 > 写 > 擦除 的优先级向 PHY 下发命令。
    """

    def __init__(self):
        self._onfly_schedule_req_no = 0
        # Scheduling priority order (highest first)
        self.sched_priority = [
            "mapping_read",
            "user_search",
            "user_compute",
            "user_read",
            "mapping_write",
            "user_write",
            "gc_read",
            "gc_write",
            "gc_erase",
        ]
        # queues[channel][chip][type] = list of Transaction
        self.queues = [
            [{key: [] for key in self.sched_priority}
             for _ in range(CHIP_PER_CHANNEL)]
            for _ in range(CHANNEL_NO)
        ]
        self.block_manager = Block_Manager()
        self.channel_no = CHANNEL_NO
        self.chip_no_per_channel = CHIP_PER_CHANNEL
        self.round_robin_turn = [0] * self.channel_no
        self.PHY = PHY()
        # Register channel/chip idle callbacks so PHY can trigger re-scheduling
        self.PHY.connect_channel_idle_signal(self._on_channel_idle)
        self.PHY.connect_chip_idle_signal(self._on_chip_idle)

    # ── Batch submission API ──────────────────────────────────────────────────

    def Prepare_trans_submission(self):
        """Open a submission batch; must be paired with Schedule()."""
        self._onfly_schedule_req_no += 1

    def Submit_trans(self, trans):
        """Enqueue a transaction to the appropriate per-chip priority queue."""
        channel = trans.address[0]
        chip    = trans.address[1]
        self.queues[channel][chip][trans.type].append(trans)

    def Schedule(self):
        """Close batch and, if all batches are closed, drive scheduling.

        对标 TSU_OutOfOrder::Schedule()。
        """
        self._onfly_schedule_req_no -= 1
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
            if chip_id_check not in self._chip_bkes:
                raise ValueError(f"Chip {chip_id_check} not found in PHY while broadcasting channel idle")
            bke = self._chip_bkes[chip_id_check]
            if not bke._waiting_data_out:
                continue
            self.PHY._broadcast_channel_idle(channel_id)
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
        if self.is_static_chip(chip_id):
            # SEARCH/COMPUTE-dedicated chip: handled separately
            if self.try_compute(chip_id):
                return True
            if self.try_search(chip_id):
                return True
            return False
        if self.try_read(chip_id):
            return True
        if self.try_write(chip_id):
            return True
        if self.try_erase(chip_id):
            return True
        return False

    # ── Per-type scheduling methods ───────────────────────────────────────────

    def try_read(self, chip_id) -> bool:
        """Try to issue a read command to the chip.

        对标 TSU_OutOfOrder::service_read_transaction()。
        Checks chip status and, if necessary, suspends an ongoing WRITE/ERASE.
        Picks the two highest-priority non-empty read queues as q1 / q2.
        """
        chip_bke = self.PHY.get_chip_bke(chip_id)
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
            if "read" not in key:
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
        chip_bke = self.PHY.get_chip_bke(chip_id)
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
        q1 = None
        for key in self.sched_priority:
            if "write" not in key:
                continue
            if chip_queues[key]:
                q1 = chip_queues[key]
                break

        if q1 is None:
            return False
        self.issue_command(chip_id, q1, None, suspension_required)
        return True

    def try_erase(self, chip_id) -> bool:
        """Try to issue an erase command to the chip.

        对标 TSU_OutOfOrder::service_erase_transaction()。
        Erase can only be issued when the chip is fully IDLE.
        """
        chip_bke = self.PHY.get_chip_bke(chip_id)
        if chip_bke.status != ChipStatus.IDLE:
            return False

        q = self.queues[chip_id[0]][chip_id[1]].get("gc_erase")
        if not q:
            return False
        self.issue_command(chip_id, q, None, False)
        return True

    def try_compute(self, chip_id) -> bool:
        """Try to issue a compute command to the chip.

        Compute 只能在 chip IDLE 时触发，直接选取 user_compute 队列，
        调用 issue_compute_command 下发。
        """
        chip_bke = self.PHY.get_chip_bke(chip_id)
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
        chip_bke = self.PHY.get_chip_bke(chip_id)
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
            return False

        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q1[0].address[2]
        start_page = q1[0].address[5]

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            page_id = start_page if _step == 0 else None
            plane_vector = 0
            dispatch_slots: list = []

            for tr in list(q1):
                if tr.address[2] != die_id:
                    continue
                tr_plane = tr.address[3]
                if plane_vector & (1 << tr_plane):
                    continue
                tr_page = tr.address[5]
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
                    if tr.address[2] != die_id:
                        continue
                    tr_plane = tr.address[3]
                    if plane_vector & (1 << tr_plane):
                        continue
                    tr_page = tr.address[5]
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
                for tr in dispatch_slots:
                    if tr in q1:
                        q1.remove(tr)
                    elif q2 is not None and tr in q2:
                        q2.remove(tr)
                self.PHY.send_command_to_chip(chip_id, dispatch_slots, suspension_required)
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
            raise ValueError("Issued an empty search transactions queue to PHY")

        die_no = DIE_PER_CHIP
        plane_no = PLANE_PER_DIE

        start_die = q[0].address[2]
        start_sub_plane = q[0].address[4]

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_vector = 0
            dispatch_slots: list = []

            for tr in list(q):
                if tr.address[2] != die_id:
                    continue
                tr_plane = tr.address[3]
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
                self.PHY.send_command_to_chip(chip_id, dispatch_slots, False)

    def issue_compute_command(self, chip_id, q: list) -> bool:
        """按 die-plane 粒度选取 compute transactions 并下发给 PHY。

        Compute 地址最细粒度为 address[4]（sub_plane/操作单元），address[5] 恒为 0。
        约束：每个 plane 中选中的操作单元数量不超过 COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL。
        对每个 die，收集满足约束的 transactions 后立即发给 PHY，
        找到第一个有候选的 die 后返回 True。
        """
        if not q:
            raise ValueError("Issued an empty compute transactions queue to PHY")

        die_no = DIE_PER_CHIP
        max_per_plane = COMPUTE_MAX_PARALLEL_SL * SSL_PER_SL

        start_die = q[0].address[2]

        for _step in range(die_no):
            die_id = (start_die + _step) % die_no
            plane_count: dict = {}
            dispatch_slots: list = []

            for tr in list(q):
                if tr.address[2] != die_id:
                    continue
                tr_plane = tr.address[3]
                count = plane_count.get(tr_plane, 0)
                if count >= max_per_plane:
                    continue
                tr.SuspendRequired = False
                plane_count[tr_plane] = count + 1
                dispatch_slots.append(tr)

            if dispatch_slots:
                for tr in dispatch_slots:
                    q.remove(tr)
                self.PHY.send_command_to_chip(chip_id, dispatch_slots, False)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def channel_is_busy(self, channel_id: int) -> bool:
        return self.PHY.channel_is_busy(channel_id)

    def is_static_chip(self, chip_id) -> bool:
        """Returns True if chip is dedicated to SEARCH/COMPUTE (placeholder)."""
        return (CHIP_PER_CHANNEL - chip_id[1]) <= STATIC_CHIP_PER_CHANNEL


class Address_Mapping_Domain:
    def __init__(self):
        self.cmt = CMT()
        self.gtd: dict[int, int] = {}
        self.gmt: dict[int, cmt_entry] = {}
        self.DepartingEntry = []
        self.ArrivingEntry = []
        self.tsu = TSU()

    def query(self, transaction: Transaction) -> bool:
        entry = self.cmt.query(transaction.lpa)
        if entry is not None:
            transaction.ppa = entry.ppa
            return True
        if transaction.lpa in self.gmt:
            transaction.ppa = self.gmt[transaction.lpa].ppa
            return True
        mvpn = transaction.lpa // LPA_NO_PER_MAPPING_PAGE
        if mvpn not in self.gtd:
            self.tsu.Prepare_trans_submission()
            self.tsu.Submit_trans(transaction)
            self.tsu.Schedule()
            return False
        mppn = self.gtd[mvpn]
        _ = _get_lpa_sector_in_mapping_page(transaction.lpa)
        self.tsu.Prepare_trans_submission()
        self.tsu.Submit_trans(transaction)
        self.tsu.Schedule()
        return False


class Address_Mapping_Unit:
    def __init__(self):
        self.domains = [Address_Mapping_Domain() for _ in range(NUM_OF_QUEUES)]
        self.waiting_for_mapping_trans: list = []
        self.tsu = TSU()
        self.gtd: dict[int, GTDEntry] = {}

    def translate_and_submit(self, req: Request):
        # SEARCH and COMPUTE requests don't need to be translated
        if req.type in (SEARCH, COMPUTE):
            self.tsu.Prepare_trans_submission()
            for tr in req.transaction_list:
                self.tsu.Submit_trans(tr)
            self.tsu.Schedule()
            return
        # process read and write requests
        domain = self.domains[req.sq_id or 0]
        self.tsu.Prepare_trans_submission()
        for tr in req.transaction_list:
            if domain.query(tr):
                self.tsu.Submit_trans(tr)
            else: # fail to find mapping info in cmt or gmt, need to wait for mapping info to be fetched from mapping page, add to waiting_for_mapping_trans
                self.waiting_for_mapping_trans.append(tr)
                mvpn = tr.lpa // LPA_NO_PER_MAPPING_PAGE
                if mvpn not in self.gtd: # cache miss and cannot find direction in gtd, which means this is the first time to access this lpa
                    if tr.source_req.type == READ:
                        raise ValueError("Read request accessing non-existing mapping page")
                    else: # write on a new mapping page
                        # generate mapping write transaction to add new direction in gtd
                        self.generate_mapping_write_transaction(tr, mvpn)
                        
                else: # find mvpn in gtd
                    entry = self.gtd[mvpn]
                    if entry.valid_bitmap[tr.lpa % LPA_NO_PER_MAPPING_PAGE] == 0: # access an invalid direction
                        if tr.source_req.type == READ:
                            raise ValueError("Read request accessing non-existing mapping page")
                        else: # mapping write
                            self.generate_mapping_write_transaction(tr, mvpn)
                    else: # access a valid direction
                        self.generate_mapping_read_transaction(tr, mvpn)
        self.tsu.Schedule()
        return
    
    def generate_mapping_write_transaction(self, trigger_tr: Transaction, mvpn):
        plane_address = self.get_plane_address(mvpn)
        mapping_page_address = self.block_manager.get_write_frontier(plane_address)
        entry = GTDEntry(address=mapping_page_address)
        entry.set_valid_bitmap(trigger_tr.lpa, 1)
        self.gtd[mvpn] = entry
        # get a page for user write request
        plane_address = self.get_plane_address(trigger_tr.lpa)
        page_address = self.block_manager.get_page_address(plane_address)
        trigger_tr.address = page_address
        # set relationship
        write_mapping_info_tr = Transaction_WR(source_req=trigger_tr.source_req, type="mapping_write", lpa=mvpn, mvpn=mvpn, sector_bitmap=[0] * SECTOR_PER_PAGE, 
            data=utils.translate_address_to_ppa(mapping_page_address)) # submit write mapping info transaction
        trigger_tr.related_transactions.append(write_mapping_info_tr)
        write_mapping_info_tr.related_transactions.append(trigger_tr)
        self.tsu.Submit_trans(trigger_tr)
        self.tsu.Submit_trans(write_mapping_info_tr)
        return

    def generate_mapping_read_transaction(self, trigger_tr: Transaction, mvpn):
        mapping_page_address = self.gtd[mvpn].address
        access_bitmap = [0 for _ in LPA_NO_PER_MAPPING_PAGE]
        access_bitmap[trigger_tr.lpa % LPA_NO_PER_MAPPING_PAGE // SECTOR_PER_PAGE] = 1
        read_tr = Transaction_RD(trigger_tr.source_req, address=mapping_page_address, sector_bitmap=access_bitmap)
        read_tr.related_transactions.append(trigger_tr)
        trigger_tr.related_transactions.append(read_tr)
        self.tsu.Submit_trans(trigger_tr)
        self.tsu.Submit_trans(read_tr)
        return


    def handle_mapping_read_response(self, response):
        # 用 response 更新 CMT/GMT，并继续处理 waiting_read_write_trans / waiting_search_compute_req
        pass


class FTL:
    def __init__(self):
        self.address_mapping_unit = Address_Mapping_Unit()
        self.gc_wl_manager = GC_WL_Manager()
        self.block_manager = Block_Manager()

    def handle_new_req(self, req: Request):
        self.address_mapping_unit.translate_and_submit(req)
    
    def get_static_address(self, sub_plane_id: int) -> tuple:
        sub_plane_address = sub_plane_id % (SL_PER_BLOCK * SSL_PER_SL * BLOCK_PER_PLANE)
        sub_plane_id //= SL_PER_BLOCK * SSL_PER_SL * BLOCK_PER_PLANE
        plane_address = sub_plane_id % PLANE_PER_DIE
        sub_plane_id //= PLANE_PER_DIE
        die_address = sub_plane_id % DIE_PER_CHIP
        sub_plane_id //= DIE_PER_CHIP
        chip_address = sub_plane_id % STATIC_CHIP_PER_CHANNEL
        sub_plane_id //= STATIC_CHIP_PER_CHANNEL
        channel_address = sub_plane_id % CHANNEL_NO
        return (channel_address, chip_address, die_address, plane_address, sub_plane_address, -1)
