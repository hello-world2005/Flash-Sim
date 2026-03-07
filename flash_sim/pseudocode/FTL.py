# -*- coding: utf-8 -*-
from dataclasses import dataclass

from common import *
from PHY import PHY
import utils

# ----- 常量 -----
CMT_SIZE = 4096
LPA_NO_PER_MAPPING_PAGE = 512
NUM_OF_QUEUES = 8
CHANNEL_NO = 8
CHIP_NO_PER_CHANNEL = 4


@dataclass
class cmt_entry:
    ppa: int
    dirty: bool


@dataclass
class Transaction:
    source_req: Request
    lpa: int = 0
    ppa: int = 0
    mvpn: int = 0
    bitmap: int = 0
    bank_id: int = 0  # 用于 SEARCH/COMPUTE 的 bank 标识


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
    def __init__(self):
        self._onfly_schedule_req_no = 0
        self.sched_priority = [
            "mapping_read",
            "user_search",
            "user_compute",
            "user_read",
            "mapping_write",
            "user_write",
            "gc_read",
            "gc_write",
        ]
        self.queues = [
            [{key: [] for key in self.sched_priority}
             for _ in range(CHIP_NO_PER_CHANNEL)]
            for _ in range(CHANNEL_NO)
        ]
        self.block_manager = Block_Manager()
        self.channel_no = CHANNEL_NO
        self.chip_no_per_channel = CHIP_NO_PER_CHANNEL
        self.round_robin_turn = [0] * self.channel_no
        self.PHY = PHY()

    def Prepare_trans_submission(self):
        self._onfly_schedule_req_no += 1

    def Prepare_trans_issue(self):
        self.Prepare_trans_submission()

    def Submit_trans(self, payload):
        if isinstance(payload, Transaction):
            channel, chip, die, plane, block, page = utils.translate_ppa_to_address(payload.ppa)
            trans_type = "user_read" if payload.source_req.type == READ else "user_write"
            self.read_write_queues[channel][chip][trans_type].append(payload)
        elif isinstance(payload, Request):
            if payload.type not in (SEARCH, COMPUTE):
                raise TypeError("Only SEARCH/COMPUTE req can be issued to tsu in req form")
            if payload.type == SEARCH:
                self.search_queue.append(payload)
            else:
                self.compute_queue.append(payload)

    def Schedule(self):
        self._onfly_schedule_req_no -= 1
        if self._onfly_schedule_req_no < 0:
            raise RuntimeError("onfly_schedule_req_no should not be negative")
        if self._onfly_schedule_req_no > 0:
            return
        for i in range(self.channel_no):
            for _ in range(self.chip_no_per_channel):
                chip_id = (i, self.round_robin_turn[i])
                chip_bke = self.PHY.get_chip_bke(chip_id)
                if chip_bke.status == "idle":
                    self.activate(chip_id)
                self.round_robin_turn[i] = (self.round_robin_turn[i] + 1) % self.chip_no_per_channel
                if chip_bke.status == "active":
                    break

    def _find_another_queue_for_same_transaction_type(self, chip_id: tuple, key: str):
        ch, chip = chip_id[0], chip_id[1]
        for k in self.sched_priority:
            if k == key and len(self.read_write_queues[ch][chip][k]) > 1:
                return self.read_write_queues[ch][chip][k]
        return None

    def activate(self, chip_id):
        chip_queues = self.read_write_queues[chip_id[0]][chip_id[1]]
        for key in self.sched_priority:
            if key not in ("user_search", "user_compute"):
                queue = chip_queues[key]
                if len(queue) > 0:
                    submit_queue2 = self._find_another_queue_for_same_transaction_type(chip_id, key)
                    self.issue_read_write_command(chip_id, queue, submit_queue2 or [])
                    break
            else:
                if len(self.search_queue) > 0:
                    self.issue_search_command(chip_id, self.search_queue)
                    break
                elif len(self.compute_queue) > 0:
                    self.issue_compute_command(chip_id, self.compute_queue)
                    break

    def can_issue(self, transaction: Transaction) -> bool:
        addr = utils.translate_ppa_to_address(transaction.ppa)
        return self.block_manager.is_free(addr) and self.block_manager.is_not_protected(addr)

    def issue_read_write_command(self, chip_id, submit_queue1, submit_queue2):
        pass

    def issue_search_command(self, chip_id, search_queue):
        pass

    def issue_compute_command(self, chip_id, compute_queue):
        pass


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
        self.waiting_search_compute_req: list = []
        self.waiting_read_write_trans: list = []
        self.tsu = TSU()

    def translate(self, req: Request):
        domain = self.domains[req.sq_id or 0]
        transactions = req.transaction_list
        if req.type in (READ, WRITE):
            self.tsu.Prepare_trans_submission()
            for tr in transactions:
                if domain.query(tr):
                    self.tsu.Submit_trans(tr)
                else:
                    self.waiting_read_write_trans.append(tr)
        elif req.type in (SEARCH, COMPUTE):
            all_translated = True
            for tr in transactions:
                if not domain.query(tr):
                    all_translated = False
            if all_translated:
                self.tsu.Prepare_trans_submission()
                self.tsu.Submit_trans(req)
                self.tsu.Schedule()
            else:
                self.waiting_search_compute_req.append(req)

    def handle_mapping_read_response(self, response):
        # 用 response 更新 CMT/GMT，并继续处理 waiting_read_write_trans / waiting_search_compute_req
        pass


class FTL:
    def __init__(self):
        self.address_mapping_unit = Address_Mapping_Unit()
        self.gc_wl_manager = GC_WL_Manager()
        self.block_manager = Block_Manager()

    def handle_new_req(self, req: Request):
        self.address_mapping_unit.translate(req)
