# -*- coding: utf-8 -*-
from queue import Queue
from typing import Mapping

from common import *
from FTL import FTL, Transaction
import PCIe_link
import utils
from math import ceil
from PCIe_link import PCIe_message
from Host import CQ_Entry


class HIL:
    def __init__(self, name, host, device):
        self.name = name
        self.host = host
        self.device = device
        # 使用 host.pcie_link 以便 Engine 注入后生效
        num_queues = getattr(host, "num_of_queues", 8)
        self.input_streams = [Queue() for _ in range(num_queues)]
        self.cache_manager = Cache_Manager()
        self.ftl = FTL()
        self._sq_head_tail = {(0, 0) for _ in range(num_queues)}
        self._cq_head_tail = {(0, 0) for _ in range(num_queues)}
        self.pcie_link = self.host.pcie_link

    def Start_simulation(self):
        self.ftl.Start_simulation()
        self.cache_manager.Start_simulation()

    def execute(self, event):
        assert event.type == EventType.DELIVER
        message = event.param
        self.receive_pcie_message(message)

    def segment(self, req):
        """根据 req 的 lha_start 和 size 范围拆成 transaction_list。"""
        # only segment once
        if req.transaction_list:
            return
        # search and compute operation should do address translation while segmenting
        if req.type in (RequestType.SEARCH, RequestType.COMPUTE):
            start_sub_plane_id = req.lha_start - TOT_RANDOM_SECTOR_NO
            size = req.size
            for i in range(size):
                sub_plane_id = start_sub_plane_id + i
                address = self.ftl.get_static_address(sub_plane_id)
                tr = Transaction(source_req=req, type=req.type, address=address)
                req.transaction_list.append(tr)
            return

        elif req.type in (RequestType.READ, RequestType.WRITE):
            start_lha = req.lha_start
            lha_count = req.size

            start_lpa = start_lha // SECTOR_PER_PAGE
            head_margin_sectors  = start_lha % SECTOR_PER_PAGE
            tail_margin_sectors = (SECTOR_PER_PAGE - (lha_count + head_margin_sectors)%SECTOR_PER_PAGE) % SECTOR_PER_PAGE

            lpa_count = max(1, ceil((lha_count + head_margin_sectors) / SECTOR_PER_PAGE))
            if lpa_count == 1: # only access one page
                bitmap = [0] * head_margin_sectors + [1] * lha_count + [0] * tail_margin_sectors
                tr = Transaction(source_req=req, lpa=start_lpa, bitmap=bitmap)
                req.transaction_list.append(tr)
                return
            # access multiple pages
            for i in range(lpa_count):
                lpa = start_lpa + i
                if i == 0:
                    bitmap = [1] * head_margin_sectors + [0] * (SECTOR_PER_PAGE - head_margin_sectors)
                elif i == lpa_count - 1:
                    bitmap = [1] * (lha_count + head_margin_sectors - tail_margin_sectors) + [0] * tail_margin_sectors
                else:
                    bitmap = [1] * SECTOR_PER_PAGE
                tr = Transaction(source_req=req, lpa=lpa, bitmap=bitmap)
                req.transaction_list.append(tr)

        raise ValueError("Unexpected req type!")

        
    def _on_transaction_serviced(self, tr): # handle trnasaction serviced signal from PHY
        # get source req of tr
        source_req = tr.source_req
        tr.completed = True
        if source_req.is_serviced():
            # 目前仅考虑把所有信息全部塞进CQ_Entry里，因此所有的req类型操作是一样的，都是发个CQ_Entry给Host
            payload = {"req": source_req, "status": "completed"}
            self._cq_head_tail[source_req.cq_id][1] += 1
            message = PCIe_link.PCIe_message(type=MessageType.REQ_COMP, payload=payload)
            self.pcie_link.send(message, self.host)



    def fetch_data(self, req):
        """向 host 请求 WRITE/SEARCH/COMPUTE 所需数据"""
        message = PCIe_link.PCIe_message(
            type=MessageType.WRITE_DATA_REQ if req.type == RequestType.WRITE else MessageType.SEARCH_DATA_REQ if req.type == RequestType.SEARCH else MessageType.COMPUTE_DATA_REQ,
            payload=req
        )
        self.pcie_link.send(message, self.host)

    def receive_pcie_message(self, message):
        if message.type in (MessageType.READ_REQ, MessageType.WRITE_DATA, MessageType.SEARCH_DATA, MessageType.COMPUTE_DATA):
            req = message.payload["req"]
            self.segment(req) # 将req拆分成transaction_list
            self.cache_manager.service(req) # 查询cache，如果命中则直接返回
            if req.is_serviced():
                comp_msg = PCIe_link.PCIe_message(
                    type=MessageType.REQ_COMP, payload={"req": req, "status": "completed"}
                )
                self.pcie_link.send(comp_msg, self.host)
                return
            self.ftl.handle_new_req(req)
        elif message.type in (MessageType.WRITE_REQ, MessageType.SEARCH_REQ, MessageType.COMPUTE_REQ):
            req = message.payload["req"]
            self.fetch_data(req)
            self.segment(req)
            self.ftl.handle_new_req(req)

from typing import Optional

PAGE_SIZE = 4096


class Cache:
    def __init__(self, max_entries: int = 1024):
        self._store: dict[int, bytes] = {}
        self._lru: list[int] = []
        self._max_entries = max_entries

    def get(self, lpa: int) -> Optional[bytes]:
        if lpa not in self._store:
            return None
        self._lru.remove(lpa)
        self._lru.insert(0, lpa)
        return self._store[lpa]

    def put(self, lpa: int, data: bytes) -> None:
        if lpa in self._store:
            self._lru.remove(lpa)
        else:
            while len(self._store) >= self._max_entries and self._lru:
                evict = self._lru.pop()
                del self._store[evict]
        self._store[lpa] = data
        self._lru.insert(0, lpa)

class Cache_Manager:
    def __init__(self):
        self.cache = Cache()
        self._lru_list: list[int] = []

    def Start_simulation(self):
        self.cache.Start_simulation()

    def service(self, req):
        if req.type == RequestType.READ:
            self.query_cache(req)
            return
        if req.type in (RequestType.WRITE, RequestType.SEARCH, RequestType.COMPUTE):
            self.write_cache(req)
            return
        elif req.type == RequestType.SEARCH or req.type == RequestType.COMPUTE:
            print(f"Cache manager does not support {req.type} request")
            return
        raise TypeError("Unsupposed req type")

    def query_cache(self, req):
        for tr in req.transaction_list:
            data = self.cache.get(tr.lpa)
            if data is not None:
                setattr(tr, "cached_data", data)
                req.serviced_trans += 1
        # cache miss 的 transaction 留在 list 中，由 FTL 处理

    def write_cache(self, req):
        for tr in req.transaction_list:
            data = getattr(tr, "data", b"\x00" * 4096)
            self.cache.put(tr.lpa, data)
