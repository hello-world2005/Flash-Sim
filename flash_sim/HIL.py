# -*- coding: utf-8 -*-
from collections import defaultdict
from math import ceil
from queue import Queue

from flash_sim import utils

from . import pcie_link as PCIe_link
from .FTL import FTL, Transaction
from .common import *


class HIL:
    def __init__(self, name, host, device):
        print("Initializing HIL...")
        self._construction_valid: bool = False
        self.name = name
        self.host = host
        self.device = device
        num_queues = getattr(host, "num_of_queues", 8)
        self.input_streams = [Queue() for _ in range(num_queues)]
        self.cache_manager = Cache_Manager(self)
        self.ftl: FTL
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
        return self.host.pcie_link

    def execute(self, event):
        from .common import log_execute_event

        log_execute_event(self.__class__.__name__, event)
        assert event.type == EventType.DELIVER
        message = event.param["message"]
        self.receive_pcie_message(message)

    def segment(self, req):
        """Segment a request into transaction_list only once."""
        if req.transaction_list:
            return
        if req.type in (RequestType.SEARCH, RequestType.COMPUTE, RequestType.STATIC_WRITE):
            size = req.size
            for i in range(size):
                sub_plane_id = req.lha_start + i
                address = self.ftl.get_static_address(sub_plane_id)
                lpa = utils.translate_lha_to_lpa(sub_plane_id)
                tr_type = (
                    TransactionType.USER_SEARCH
                    if req.type == RequestType.SEARCH
                    else TransactionType.USER_COMPUTE
                    if req.type == RequestType.COMPUTE
                    else TransactionType.USER_STATIC_WRITE
                )
                tr = Transaction(
                    source_req=req,
                    type=tr_type,
                    address=address,
                    lpa=lpa,
                    data_ready=req.type == RequestType.SEARCH,
                )
                req.transaction_list.append(tr)
            return

        if req.type not in (RequestType.READ, RequestType.WRITE):
            raise ValueError("Unexpected req type!")

        start_lha = req.lha_start
        lha_count = req.size

        start_lpa = start_lha // SECTOR_PER_PAGE
        head_margin_sectors = start_lha % SECTOR_PER_PAGE
        tail_margin_sectors = (SECTOR_PER_PAGE - (lha_count + head_margin_sectors) % SECTOR_PER_PAGE) % SECTOR_PER_PAGE

        lpa_count = max(1, ceil((lha_count + head_margin_sectors) / SECTOR_PER_PAGE))
        if lpa_count == 1:
            bitmap = [0] * head_margin_sectors + [1] * lha_count + [0] * tail_margin_sectors
            tr_type = TransactionType.USER_READ if req.type == RequestType.READ else TransactionType.USER_WRITE
            tr = Transaction(
                source_req=req,
                lpa=start_lpa,
                bitmap=bitmap,
                type=tr_type,
                data_ready=req.type == RequestType.READ,
            )
            req.transaction_list.append(tr)
            return

        for i in range(lpa_count):
            lpa = start_lpa + i
            if i == 0:
                bitmap = [0] * head_margin_sectors + [1] * (SECTOR_PER_PAGE - head_margin_sectors)
            elif i == lpa_count - 1:
                bitmap = [1] * (SECTOR_PER_PAGE - tail_margin_sectors) + [0] * tail_margin_sectors
            else:
                bitmap = [1] * SECTOR_PER_PAGE
            tr_type = TransactionType.USER_READ if req.type == RequestType.READ else TransactionType.USER_WRITE
            tr = Transaction(
                source_req=req,
                lpa=lpa,
                bitmap=bitmap,
                type=tr_type,
                data_ready=req.type == RequestType.READ,
            )
            req.transaction_list.append(tr)

    def _on_transaction_serviced(self, tr):
        debug_info(f"[HIL] _on_transaction_serviced: tr: {repr(tr)}")
        tr.completed = True
        self.cache_manager.on_transaction_serviced(tr)
        source_req = tr.source_req
        if source_req is None:
            return
        if source_req.is_serviced():
            req_brief = f"Request(type={source_req.type}, lha_start={source_req.lha_start}, size={source_req.size})"
            debug_info(f"[HIL] _on_transaction_serviced: source_req is serviced, sending REQ_COMP to Host: {req_brief}")
            payload = {"req": source_req, "status": "completed"}
            self.host.queue_ptrs.cq_tails[source_req.sq_id] += 1
            message = PCIe_link.PCIe_message(type=MessageType.REQ_COMP, payload=payload)
            self.pcie_link.send(message, self.host)
        else:
            debug_info("[HIL] _on_transaction_serviced: source_req is not serviced yet")

    def fetch_data(self, req):
        message_type = {
            RequestType.WRITE: MessageType.WRITE_DATA_REQ,
            RequestType.SEARCH: MessageType.SEARCH_DATA_REQ,
            RequestType.COMPUTE: MessageType.COMPUTE_DATA_REQ,
            RequestType.STATIC_WRITE: MessageType.STATIC_WRITE_DATA_REQ,
        }.get(req.type)
        if message_type is None:
            raise ValueError(f"Unexpected data-fetch req type: {req.type}")
        message = PCIe_link.PCIe_message(type=message_type, payload={"req": req})
        self.pcie_link.send(message, self.host)

    def receive_pcie_message(self, message):
        if message.type == MessageType.READ_REQ:
            req = message.payload["req"]
            self.segment(req)
            self.cache_manager.query_cache(req)
            if req.is_serviced():
                self._complete_request(req)
                return
            self.ftl.handle_new_req(req)
        elif message.type in (
            MessageType.WRITE_REQ,
            MessageType.SEARCH_REQ,
            MessageType.COMPUTE_REQ,
            MessageType.STATIC_WRITE_REQ,
        ):
            req = message.payload["req"]
            self.segment(req)
            if req.type in (RequestType.WRITE, RequestType.STATIC_WRITE):
                self.cache_manager.register_write_request(req)
            self.fetch_data(req)
            if req.type in (RequestType.SEARCH, RequestType.COMPUTE):
                self.ftl.handle_new_req(req)
        elif message.type in (
            MessageType.WRITE_DATA,
            MessageType.SEARCH_DATA,
            MessageType.COMPUTE_DATA,
            MessageType.STATIC_WRITE_DATA,
        ):
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
        self.user_entries: dict[int, dict] = {}
        self.static_entries: dict[int, dict] = {}

    @property
    def lines(self) -> dict[int, int]:
        lines: dict[int, int] = {}
        for lpa, entry in self.user_entries.items():
            ready_bitmap = entry["ready_bitmap"]
            payload = entry["payload"]
            for sector_idx in range(SECTOR_PER_PAGE):
                if ready_bitmap[sector_idx] == 1:
                    lines[lpa * SECTOR_PER_PAGE + sector_idx] = payload[sector_idx]
        for line_addr, entry in self.static_entries.items():
            if entry["ready"]:
                value = entry["payload"][0] if entry["payload"] else INVALID_DATA
                lines[line_addr] = value
        return lines

    def free_lines(self) -> int:
        return self._max_lines - len(self.lines)

    def clear(self):
        self.user_entries.clear()
        self.static_entries.clear()


class Cache_Manager:
    def __init__(self, hil: HIL):
        self.hil = hil
        self.cache = Data_Cache()

    @property
    def pending_user_pages(self):
        return self.cache.user_entries

    @property
    def pending_static_pages(self):
        return self.cache.static_entries

    def _line_addr_of_static(self, tr: Transaction) -> int:
        return tr.lpa

    def _new_user_entry(self, sq_id: int) -> dict:
        return {
            "sq_id": sq_id,
            "bitmap": [0] * SECTOR_PER_PAGE,
            "ready_bitmap": [0] * SECTOR_PER_PAGE,
            "payload": [INVALID_DATA] * SECTOR_PER_PAGE,
        }

    def _new_static_entry(self, sq_id: int, tr: Transaction) -> dict:
        return {
            "sq_id": sq_id,
            "lpa": tr.lpa,
            "address": tr.address,
            "bitmap": [1],
            "ready": False,
            "payload": [INVALID_DATA],
        }

    def _user_entry_covers_transaction(self, entry: dict, tr: Transaction) -> bool:
        for i in range(SECTOR_PER_PAGE):
            if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                continue
            if entry["bitmap"][i] == 0:
                return False
        return True

    def _user_entry_is_fully_ready(self, entry: dict) -> bool:
        for i in range(SECTOR_PER_PAGE):
            if entry["bitmap"][i] == 1 and entry["ready_bitmap"][i] == 0:
                return False
        return True

    def _count_new_ready_lines(self, req: Request) -> int:
        if req.type == RequestType.WRITE:
            new_ready_lines = 0
            for tr in req.transaction_list:
                entry = self.cache.user_entries.get(tr.lpa, self._new_user_entry(req.sq_id if req.sq_id is not None else 0))
                for i in range(SECTOR_PER_PAGE):
                    if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                        continue
                    if entry["ready_bitmap"][i] == 0:
                        new_ready_lines += 1
            return new_ready_lines

        if req.type == RequestType.STATIC_WRITE:
            new_ready_lines = 0
            for tr in req.transaction_list:
                line_addr = self._line_addr_of_static(tr)
                entry = self.cache.static_entries.get(line_addr)
                if entry is None or not entry["ready"]:
                    new_ready_lines += 1
            return new_ready_lines

        return 0

    def register_write_request(self, req: Request):
        if req.type == RequestType.WRITE:
            self._register_user_write(req)
        elif req.type == RequestType.STATIC_WRITE:
            self._register_static_write(req)

    def query_cache(self, req: Request):
        misses = []
        for tr in req.transaction_list:
            if tr.type != TransactionType.USER_READ:
                misses.append(tr)
                continue
            entry = self.cache.user_entries.get(tr.lpa)
            if entry is None or not self._user_entry_covers_transaction(entry, tr):
                misses.append(tr)
                continue
            payload = [INVALID_DATA] * SECTOR_PER_PAGE
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                if entry["ready_bitmap"][i] == 1:
                    payload[i] = entry["payload"][i]
            tr.payload = payload
            tr.completed = True
        req.transaction_list = misses

    def cache_write(self, req: Request):
        if req.type not in (RequestType.WRITE, RequestType.STATIC_WRITE):
            return
        self.register_write_request(req)
        new_lines = self._count_new_ready_lines(req)
        if new_lines > self.cache.free_lines():
            self.write_flush()
        if new_lines > self.cache.free_lines():
            raise ValueError("Incoming write data exceeds DATA_CACHE_CAP")
        if req.type == RequestType.WRITE:
            self._hydrate_user_write(req)
        else:
            self._hydrate_static_write(req)

    def _register_user_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            entry = self.cache.user_entries.setdefault(tr.lpa, self._new_user_entry(sq_id))
            entry["sq_id"] = sq_id
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                entry["bitmap"][i] = 1
                entry["ready_bitmap"][i] = 0
                entry["payload"][i] = INVALID_DATA

    def _hydrate_user_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            entry = self.cache.user_entries.setdefault(tr.lpa, self._new_user_entry(sq_id))
            entry["sq_id"] = sq_id
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                if i >= len(tr.payload):
                    continue
                entry["bitmap"][i] = 1
                entry["ready_bitmap"][i] = 1
                entry["payload"][i] = tr.payload[i]

    def _register_static_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            line_addr = self._line_addr_of_static(tr)
            self.cache.static_entries[line_addr] = self._new_static_entry(sq_id, tr)

    def _hydrate_static_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            line_addr = self._line_addr_of_static(tr)
            entry = self.cache.static_entries.setdefault(line_addr, self._new_static_entry(sq_id, tr))
            entry["sq_id"] = sq_id
            entry["lpa"] = tr.lpa
            entry["address"] = tr.address
            entry["ready"] = True
            entry["payload"] = [tr.payload[0] if tr.payload else INVALID_DATA]

    def write_flush(self):
        flushable_user_pages = {
            lpa: entry
            for lpa, entry in self.cache.user_entries.items()
            if self._user_entry_is_fully_ready(entry)
        }
        flushable_static_pages = {
            line_addr: entry
            for line_addr, entry in self.cache.static_entries.items()
            if entry["ready"]
        }
        if not flushable_user_pages and not flushable_static_pages:
            return

        amu = self.hil.ftl.address_mapping_unit
        tsu = self.hil.ftl.tsu

        user_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        flushed_user_count = 0
        for lpa, entry in flushable_user_pages.items():
            user_flush_reqs[entry["sq_id"]].append(
                Transaction(
                    source_req=None,
                    type=TransactionType.USER_WRITE,
                    lpa=lpa,
                    bitmap=list(entry["bitmap"]),
                    payload=list(entry["payload"]),
                    data_ready=True,
                    cache_flush_generated=True,
                )
            )
            flushed_user_count += 1
        if flushed_user_count > 0:
            tsu.start_cache_pressure_drain(flushed_user_count)
        for sq_id, transaction_list in user_flush_reqs.items():
            amu.translate_and_submit(
                Request(
                    type=RequestType.WRITE,
                    sq_id=sq_id,
                    transaction_list=transaction_list,
                )
            )
        for lpa in flushable_user_pages:
            self.cache.user_entries.pop(lpa, None)

        static_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        for entry in flushable_static_pages.values():
            static_flush_reqs[entry["sq_id"]].append(
                Transaction(
                    source_req=None,
                    type=TransactionType.USER_STATIC_WRITE,
                    lpa=entry["lpa"],
                    address=entry["address"],
                    bitmap=list(entry["bitmap"]),
                    payload=list(entry["payload"]),
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
        for line_addr in flushable_static_pages:
            self.cache.static_entries.pop(line_addr, None)

    def on_transaction_serviced(self, tr: Transaction):
        if tr.type == TransactionType.USER_WRITE and tr.cache_flush_generated:
            self.hil.ftl.tsu.finish_cache_pressure_write()
