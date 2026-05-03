# -*- coding: utf-8 -*-
from ipaddress import AddressValueError
from queue import Queue

from flash_sim import utils
from .common import *
from .FTL import FTL, Transaction
from . import pcie_link as PCIe_link
from math import ceil
from typing import Optional
from collections import defaultdict


class HIL:
    def __init__(self, name, host, device):
        print("Initializing HIL...")
        self._construction_valid: bool = False
        self.name = name
        self.host = host
        self.device = device
        # 使用 host.pcie_link 以便 Engine 注入后生效
        num_queues = getattr(host, "num_of_queues", 8)
        self.input_streams = [Queue() for _ in range(num_queues)]
        self.cache_manager = Cache_Manager(self)
        self.ftl : FTL
        print("HIL initialization complete.")
    
    def Validate_construction(self):
        if self._construction_valid:
            return
        assert self.host is not None, "HIL host is not set"
        assert self.device is not None, "HIL device is not set"
        assert self.input_streams is not None, "HIL input_streams is not set"
        assert self.cache_manager is not None, "HIL cache_manager is not set"
        assert self.ftl is not None, "HIL ftl is not set"
        self._construction_valid = True
        self.ftl.Validate_construction()
        print("HIL construction validation complete.")

    @property
    def pcie_link(self):
        """延迟获取 pcie_link，确保 Engine 注入后才使用。"""
        return self.host.pcie_link

    def execute(self, event):
        from .common import log_execute_event
        log_execute_event(self.__class__.__name__, event)
        assert event.type == EventType.DELIVER
        message = event.param["message"]
        self.receive_pcie_message(message)

    def segment(self, req):
        """根据 req 的 lha_start 和 size 范围拆成 transaction_list。"""
        # only segment once
        if req.transaction_list:
            return
        # search, compute and static write operation should do address translation while segmenting
        if req.type in (RequestType.SEARCH, RequestType.COMPUTE, RequestType.STATIC_WRITE):
            size = req.size
            for i in range(size):
                sub_plane_id = req.lha_start + i
                address = self.ftl.get_static_address(sub_plane_id)
                lpa  = utils.translate_lha_to_lpa(sub_plane_id)
                tr_type = TransactionType.USER_SEARCH if req.type == RequestType.SEARCH else TransactionType.USER_COMPUTE if req.type == RequestType.COMPUTE else TransactionType.USER_STATIC_WRITE if req.type == RequestType.STATIC_WRITE else None
                tr = Transaction(source_req=req, type=tr_type, address=address, lpa=lpa, data_ready=True if req.type == RequestType.SEARCH else False)
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
                tr_type = None
                if req.type == RequestType.READ:
                    tr_type = TransactionType.USER_READ
                elif req.type == RequestType.WRITE:
                    tr_type = TransactionType.USER_WRITE
                else:
                    raise ValueError(f"Invalid request type: {req.type}")
                tr = Transaction(source_req=req, lpa=start_lpa, bitmap=bitmap, type=tr_type, data_ready=True if req.type == RequestType.READ else False)
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
                tr_type = None
                if req.type == RequestType.READ:
                    tr_type = TransactionType.USER_READ
                elif req.type == RequestType.WRITE:
                    tr_type = TransactionType.USER_WRITE
                else:
                    raise ValueError(f"Invalid request type: {req.type}")
                tr = Transaction(source_req=req, lpa=lpa, bitmap=bitmap, type=tr_type, data_ready=True if req.type == RequestType.READ else False)
                req.transaction_list.append(tr)
        else:
            raise ValueError("Unexpected req type!")

        
    def _on_transaction_serviced(self, tr): # handle trnasaction serviced signal from PHY
        # get source req of tr
        debug_info(f"[HIL] _on_transaction_serviced: tr: {repr(tr)}")
        source_req = tr.source_req
        tr.completed = True
        if source_req is None:
            return
        if source_req.is_serviced():
            req_brief = f"Request(type={source_req.type}, lha_start={source_req.lha_start}, size={source_req.size})"
            debug_info(f"[HIL] _on_transaction_serviced: source_req is serviced, sending REQ_COMP to Host: {req_brief}")
            # 目前仅考虑把所有信息全部塞进CQ_Entry里，因此所有的req类型操作是一样的，都是发个CQ_Entry给Host
            payload = {"req": source_req, "status": "completed"}
            self.host.queue_ptrs.cq_tails[source_req.sq_id] += 1
            message = PCIe_link.PCIe_message(type=MessageType.REQ_COMP, payload=payload)
            self.pcie_link.send(message, self.host)
        else:
            debug_info(f"[HIL] _on_transaction_serviced: source_req is not serviced yet")



    def fetch_data(self, req):
        """向 host 请求 WRITE/SEARCH/COMPUTE 所需数据"""
        message = PCIe_link.PCIe_message(
            type=MessageType.WRITE_DATA_REQ if req.type == RequestType.WRITE else MessageType.SEARCH_DATA_REQ if req.type == RequestType.SEARCH else MessageType.COMPUTE_DATA_REQ,
            payload={"req": req}
        )
        self.pcie_link.send(message, self.host)

    def receive_pcie_message(self, message):
        if message.type == MessageType.READ_REQ:
            req = message.payload["req"]
            self.segment(req) # 将req拆分成transaction_list
            self.cache_manager.query_cache(req)
            if req.is_serviced():
                self._complete_request(req)
                return
            self.ftl.handle_new_req(req)
        elif message.type in (MessageType.WRITE_REQ, MessageType.SEARCH_REQ, MessageType.COMPUTE_REQ, MessageType.STATIC_WRITE_REQ):
            req = message.payload["req"]
            self.segment(req)
            self.fetch_data(req)
            if req.type in (RequestType.SEARCH, RequestType.COMPUTE):
                self.ftl.handle_new_req(req)
        elif message.type in (MessageType.WRITE_DATA, MessageType.SEARCH_DATA, MessageType.COMPUTE_DATA, MessageType.STATIC_WRITE_DATA):
            req = message.payload["req"]
            data = message.payload["data"]
            debug_info(f"[HIL] received data for req: {req}")
            self._tile_data(req, data)
            for tr in req.transaction_list:
                tr.data_ready = True
            if req.type in (RequestType.WRITE, RequestType.STATIC_WRITE):
                self.cache_manager.cache_write(req)
                self._complete_request(req)
            else:
                self.ftl.tsu.Prepare_trans_submission()
                self.ftl.tsu.Schedule()
        else:
            raise ValueError(f"Unexpected message type for HIL: {message.type}")

    def _complete_request(self, req: Request):
        payload = {"req": req, "status": "completed"}
        if req.sq_id is not None:
            self.host.queue_ptrs.cq_tails[req.sq_id] += 1
        comp_msg = PCIe_link.PCIe_message(type=MessageType.REQ_COMP, payload=payload)
        self.pcie_link.send(comp_msg, self.host)

    def _tile_data(self, req, data):
        debug_info(f"[HIL] <_tile_data> req: {req}, data: {data}")
        if req.type == RequestType.STATIC_WRITE:
            for idx, tr in enumerate(req.transaction_list):
                value = data[idx] if idx < len(data) else INVALID_DATA
                tr.bitmap = [1]
                tr.payload = [value]
            return
        cntr = 0
        for tr in req.transaction_list:
            payload = [INVALID_DATA] * SECTOR_PER_PAGE
            for i in range(SECTOR_PER_PAGE):
                if i < len(tr.bitmap) and tr.bitmap[i] == 1:
                    payload[i] = data[cntr]
                    cntr += 1
            tr.payload = payload

class Data_Cache:
    def __init__(self, cache_line_size: int = DATA_CACHE_LINE_SIZE, capacity: int = DATA_CACHE_CAP):
        if cache_line_size <= 0:
            raise ValueError("cache_line_size must be positive")
        if capacity <= 0 or capacity % cache_line_size != 0:
            raise ValueError("DATA_CACHE_CAP must be an integer multiple of cache_line_size")
        self.cache_line_size = cache_line_size
        self.capacity = capacity
        self._max_lines = capacity // cache_line_size
        self.lines: dict[int, int] = {}

    def free_lines(self) -> int:
        return self._max_lines - len(self.lines)

    def clear(self):
        self.lines.clear()


class Cache_Manager:
    def __init__(self, hil: HIL):
        self.hil = hil
        self.cache = Data_Cache()
        self.pending_user_pages: dict[int, dict] = {}
        self.pending_static_pages: dict[int, dict] = {}

    def _line_addr_of_user(self, tr: Transaction, sector_idx: int) -> int:
        return tr.lpa * SECTOR_PER_PAGE + sector_idx

    def _line_addr_of_static(self, tr: Transaction) -> int:
        return tr.lpa

    def query_cache(self, req: Request):
        misses = []
        for tr in req.transaction_list:
            if tr.type != TransactionType.USER_READ:
                misses.append(tr)
                continue
            payload = [INVALID_DATA] * SECTOR_PER_PAGE
            hit = True
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                line_addr = self._line_addr_of_user(tr, i)
                if line_addr not in self.cache.lines:
                    hit = False
                    break
                payload[i] = self.cache.lines[line_addr]
            if hit:
                tr.payload = payload
                tr.completed = True
            else:
                misses.append(tr)
        req.transaction_list = misses

    def _count_new_cache_lines(self, req: Request) -> int:
        new_line_addrs = set()
        if req.type == RequestType.WRITE:
            for tr in req.transaction_list:
                for i in range(SECTOR_PER_PAGE):
                    if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                        continue
                    line_addr = self._line_addr_of_user(tr, i)
                    if line_addr not in self.cache.lines:
                        new_line_addrs.add(line_addr)
        elif req.type == RequestType.STATIC_WRITE:
            for tr in req.transaction_list:
                line_addr = self._line_addr_of_static(tr)
                if line_addr not in self.cache.lines:
                    new_line_addrs.add(line_addr)
        return len(new_line_addrs)

    def cache_write(self, req: Request):
        if req.type not in (RequestType.WRITE, RequestType.STATIC_WRITE):
            return
        new_lines = self._count_new_cache_lines(req)
        if new_lines > self.cache.free_lines():
            self.write_flush()
        if new_lines > self.cache.free_lines():
            raise ValueError("Incoming write data exceeds DATA_CACHE_CAP")
        if req.type == RequestType.WRITE:
            self._cache_user_write(req)
        else:
            self._cache_static_write(req)

    def _cache_user_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            cached_page = self.pending_user_pages.setdefault(
                tr.lpa,
                {
                    "sq_id": sq_id,
                    "bitmap": [0] * SECTOR_PER_PAGE,
                    "payload": [INVALID_DATA] * SECTOR_PER_PAGE,
                },
            )
            cached_page["sq_id"] = sq_id
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                if i >= len(tr.payload):
                    continue
                line_addr = self._line_addr_of_user(tr, i)
                self.cache.lines[line_addr] = tr.payload[i]
                cached_page["bitmap"][i] = 1
                cached_page["payload"][i] = tr.payload[i]

    def _cache_static_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            line_addr = self._line_addr_of_static(tr)
            value = tr.payload[0] if tr.payload else INVALID_DATA
            self.cache.lines[line_addr] = value
            self.pending_static_pages[line_addr] = {
                "sq_id": sq_id,
                "lpa": tr.lpa,
                "address": tr.address,
                "bitmap": [1],
                "payload": [value],
            }

    def write_flush(self):
        if not self.pending_user_pages and not self.pending_static_pages:
            return
        amu = self.hil.ftl.address_mapping_unit
        user_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        for lpa, cached_page in self.pending_user_pages.items():
            user_flush_reqs[cached_page["sq_id"]].append(
                Transaction(
                    source_req=None,
                    type=TransactionType.USER_WRITE,
                    lpa=lpa,
                    bitmap=list(cached_page["bitmap"]),
                    payload=list(cached_page["payload"]),
                    data_ready=True,
                )
            )
        for sq_id, transaction_list in user_flush_reqs.items():
            amu.translate_and_submit(
                Request(
                    type=RequestType.WRITE,
                    sq_id=sq_id,
                    transaction_list=transaction_list,
                )
            )

        static_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        for cached_page in self.pending_static_pages.values():
            static_flush_reqs[cached_page["sq_id"]].append(
                Transaction(
                    source_req=None,
                    type=TransactionType.USER_STATIC_WRITE,
                    lpa=cached_page["lpa"],
                    address=cached_page["address"],
                    bitmap=list(cached_page["bitmap"]),
                    payload=list(cached_page["payload"]),
                    data_ready=True,
                )
            )
        for sq_id, transaction_list in static_flush_reqs.items():
            amu.translate_and_submit(
                Request(
                    type=RequestType.STATIC_WRITE,
                    sq_id=sq_id,
                    transaction_list=transaction_list,
                )
            )

        self.pending_user_pages.clear()
        self.pending_static_pages.clear()
        self.cache.clear()
