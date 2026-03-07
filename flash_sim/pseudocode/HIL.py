# -*- coding: utf-8 -*-
from queue import Queue

from common import *
from FTL import FTL, Transaction
from Cache import Cache
import PCIe_link
import utils


class HIL(sim_object):
    def __init__(self, name, host, device):
        self.name = name
        self.host = host
        self.device = device
        # 使用 host.pcie_link 以便 Engine 注入后生效
        self._host = host
        num_queues = getattr(host, "num_of_queues", 8)
        self.input_streams = [Queue() for _ in range(num_queues)]
        self.cache_manager = Cache_Manager()
        self.ftl = FTL()
        self._sq_head_tail = {}
        self._cq_head_tail = {}

    def execute(self, event):
        self.receive_pcie_message(event.param)

    def segment(self, req):
        """根据 req 的 lpa 范围拆成 transaction_list。"""
        if req.transaction_list:
            return
        start = getattr(req, "lpa_start", 0)
        count = getattr(req, "lpa_count", 1)
        if req.type in (READ, WRITE):
            for i in range(count):
                lpa = start + i
                tr = Transaction(source_req=req, lpa=lpa, bitmap=1)
                tr.mvpn = lpa // 512
                req.transaction_list.append(tr)
        elif req.type == SEARCH:
            start_bank_id = utils.translate_lpa_to_search_bank_id(start)
            bank_count = count // PAGE_NO_PER_SEARCH_BANK
            for i in range(count):
                bank_id = start_bank_id + i
                tr = Transaction(source_req=req, bank_id=bank_id, bitmap=1)
                req.transaction_list.append(tr)
        elif req.type == COMPUTE:
            start_bank_id = utils.translate_lpa_to_compute_bank_id(start)
            bank_count = count // PAGE_NO_PER_COMPUTE_BANK
            for i in range(count):
                bank_id = start_bank_id + i
                tr = Transaction(source_req=req, bank_id=bank_id, bitmap=1)
                req.transaction_list.append(tr)

    def fetch_data(self, req):
        """向 host 请求 WRITE/SEARCH/COMPUTE 所需数据（占位）。"""
        pass

    def sq_update(self, sq_id, new_head, new_tail):
        self._sq_head_tail[sq_id] = (new_head, new_tail)

    def cq_update(self, cq_id, new_head, new_tail):
        self._cq_head_tail[cq_id] = (new_head, new_tail)

    def receive_pcie_message(self, message):
        target_queue = self.input_streams[message.sq_id]
        req = message.source_req
        target_queue.put(req)
        if message.type in (READ_REQ, WRITE_DATA, SEARCH_INPUT, COMPUTE_INPUT):
            self.segment(req)
            self.cache_manager.service(req)
            if req.is_serviced():
                comp_msg = PCIe_link.PCIe_message(
                    type=REQ_COMP, payload=None, source_req=req, sq_id=req.sq_id
                )
                self._host.pcie_link.send(comp_msg, self.host)
                return
            self.ftl.handle_new_req(req)
        elif message.type in (WRITE_REQ, SEARCH_REQ, COMPUTE_REQ):
            self.fetch_data(req)
            self.segment(req)
            self.ftl.handle_new_req(req)
        elif message.type == SQ_INFORM:
            param = message.payload
            self.sq_update(param["sq_id"], param["new_head"], param["new_tail"])
        elif message.type == CQ_INFORM:
            param = message.payload
            self.cq_update(param["cq_id"], param["new_head"], param["new_tail"])


class Cache_Manager:
    def __init__(self):
        self.cache = Cache()
        self._lru_list: list[int] = []

    def service(self, req):
        if req.type == READ:
            self.query_cache(req)
            return
        if req.type in (WRITE, SEARCH, COMPUTE):
            self.write_cache(req)
            return
        raise TypeError("Unsupported req type for cache manager")

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
