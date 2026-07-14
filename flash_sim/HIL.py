# -*- coding: utf-8 -*-
from collections import defaultdict
from math import ceil
from queue import Queue

from flash_sim import utils

from . import PCIe_link as PCIe_link
from .FTL import FTL, Transaction
from .common import *


class HIL:
    def __init__(
        self,
        name,
        host,
        device,
        cache_bypass: bool = False,
        data_cache_capacity: int | None = None,
        wl_per_string: int = WL_PER_STRING,
    ):
        if not QUIET:
            print("Initializing HIL...")
        self._construction_valid: bool = False
        self.name = name
        self.host = host
        self.device = device
        self.cache_bypass = cache_bypass
        self.wl_per_string = wl_per_string
        num_queues = getattr(host, "num_of_queues", 8)
        self.input_streams = [Queue() for _ in range(num_queues)]
        self.cache_manager = Cache_Manager(self, data_cache_capacity=data_cache_capacity)
        self.ftl: FTL
        if not QUIET:
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
        if not QUIET:
            print("HIL construction validation complete.")

    @property
    def pcie_link(self):
        return self.host.pcie_link

    def _static_region_end_exclusive(self) -> int:
        return STATIC_BASE_LHA + (
            CHANNEL_NO
            * STATIC_CHIP_PER_CHANNEL
            * DIE_PER_CHIP
            * PLANE_PER_DIE
            * BLOCK_PER_PLANE
            * SL_PER_BLOCK
            * SSL_PER_SL
        )

    def _range_is_static(self, start_lha: int, size: int) -> bool:
        end_lha = start_lha + size
        return STATIC_BASE_LHA <= start_lha and end_lha <= self._static_region_end_exclusive()

    def _range_is_random_access(self, start_lha: int, size: int) -> bool:
        end_lha = start_lha + size
        return 0 <= start_lha and end_lha <= STATIC_BASE_LHA

    def _request_brief(self, req: Request) -> str:
        return (
            f"type={req.type.value} sq_id={req.sq_id} "
            f"lha_start={req.lha_start} size={req.size}"
        )

    def _validate_request_domain(self, req: Request):
        if req.type == RequestType.COMPUTE:
            if isinstance(req.selected_wl, bool) or not isinstance(req.selected_wl, int):
                raise RequestFailure("COMPUTE request requires integer selected_wl")
            if not 0 <= req.selected_wl < self.wl_per_string:
                raise RequestFailure(
                    f"COMPUTE selected_wl must be in [0, {self.wl_per_string})"
                )

        if req.type == RequestType.WRITE:
            if not self._range_is_random_access(req.lha_start, req.size):
                raise RequestFailure(
                    "WRITE request must stay in random-access area; use STATIC_WRITE for static-area writes"
                )
            return

        if req.type in (RequestType.SEARCH, RequestType.COMPUTE, RequestType.STATIC_WRITE):
            if not self._range_is_static(req.lha_start, req.size):
                raise RequestFailure(
                    f"{req.type.value} request must stay in static area"
                )

    def _finalize_request(self, req: Request, status: str, error_message: str | None = None):
        if req.completion_sent:
            return

        req.status = status
        req.error_message = error_message
        req.completion_sent = True

        if status == REQUEST_STATUS_ERROR:
            for tr in req.transaction_list:
                if tr.completed:
                    if tr.error_message is None:
                        tr.error_message = error_message
                    if not tr.failed:
                        tr.failed = True
                    continue
                tr.completed = True
                tr.failed = True
                tr.error_message = error_message

        debug_info(
            f"[HIL] REQ_COMP {self._request_brief(req)} "
            f"status={status} error_message={error_message!r}"
        )
        payload = {
            "req": req,
            "status": status,
            "error_message": error_message,
        }
        if status == REQUEST_STATUS_SUCCESS and req.type == RequestType.READ:
            # MQSim first DMA-writes the read payload to host memory and then
            # writes the completion queue entry.  Enqueue in that order on the
            # same device-to-host PCIe direction so REQ_COMP becomes visible
            # only after the payload transfer has completed.
            read_data_msg = PCIe_link.PCIe_message(
                type=MessageType.READ_RES_SEND_BACK,
                payload={
                    "req": req,
                    "address": req.lha_start,
                    "data": [INVALID_DATA] * req.size,
                },
            )
            self.pcie_link.send(read_data_msg, self.host)
        comp_msg = PCIe_link.PCIe_message(type=MessageType.REQ_COMP, payload=payload)
        self.pcie_link.send(comp_msg, self.host)

    def _complete_request(self, req: Request):
        self._finalize_request(req, REQUEST_STATUS_SUCCESS, None)

    def _fail_request(self, req: Request, error_message: str):
        self._finalize_request(req, REQUEST_STATUS_ERROR, error_message)

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
                    data_ready=False,
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
        if not QUIET:
            debug_info(f"[HIL] _on_transaction_serviced: tr: {repr(tr)}")
        tr.completed = True
        self.cache_manager.on_transaction_serviced(tr)
        source_req = tr.source_req
        if source_req is None:
            return
        if source_req.completion_sent:
            debug_info("[HIL] _on_transaction_serviced: source_req already completed")
            return
        if tr.failed:
            self._fail_request(source_req, tr.error_message or "request transaction failed")
            return
        if source_req.is_serviced():
            debug_info(
                "[HIL] _on_transaction_serviced: source_req is serviced, sending success REQ_COMP"
            )
            self._complete_request(source_req)
            return
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

    def _ack_request_received(self, req):
        """Send *_RECEIVED message to Host to release the SQ entry and IO flow."""
        ack_type = {
            RequestType.WRITE: MessageType.WRITE_DATA_RECEIVED,
            RequestType.READ: MessageType.READ_REQ_RECEIVED,
            RequestType.SEARCH: MessageType.SEARCH_DATA_RECEIVED,
            RequestType.COMPUTE: MessageType.COMPUTE_DATA_RECEIVED,
            RequestType.STATIC_WRITE: MessageType.WRITE_DATA_RECEIVED,
        }.get(req.type)
        if ack_type is None:
            return
        ack_msg = PCIe_link.PCIe_message(
            type=ack_type,
            payload={"sq_id": req.sq_id},
        )
        self.pcie_link.send(ack_msg, self.host)
        debug_info(
            f"[HIL] sent {ack_type.value} sq_id={req.sq_id} "
            f"for req {self._request_brief(req)}"
        )

    def receive_pcie_message(self, message):
        req = message.payload.get("req") if hasattr(message, "payload") else None
        try:
            if message.type == MessageType.READ_REQ:
                req = message.payload["req"]
                self._ack_request_received(req)
                self.segment(req)
                blocked_by_cache = self.cache_manager.query_cache(req)
                if req.is_serviced():
                    self._complete_request(req)
                    return
                if blocked_by_cache:
                    return
                self.ftl.handle_new_req(req)
            elif message.type in (
                MessageType.WRITE_REQ,
                MessageType.SEARCH_REQ,
                MessageType.COMPUTE_REQ,
                MessageType.STATIC_WRITE_REQ,
            ):
                req = message.payload["req"]
                self._validate_request_domain(req)
                self.segment(req)
                if req.type in (RequestType.WRITE, RequestType.STATIC_WRITE):
                    if not self.cache_bypass:
                        try:
                            self.cache_manager.register_write_request(req)
                        except ValueError:
                            req.cache_forced_bypass = True
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
                if not QUIET:
                    debug_info(f"[HIL] received data for req: {req}")
                self._tile_data(req, data)
                self._ack_request_received(req)
                for tr in req.transaction_list:
                    tr.data_ready = True
                if req.type in (RequestType.WRITE, RequestType.STATIC_WRITE):
                    if self.cache_bypass or req.cache_forced_bypass:
                        # Bypass cache: submit directly to FTL→TSU→PHY→NAND.
                        self.ftl.handle_new_req(req)
                    else:
                        try:
                            resumed_reqs = self.cache_manager.cache_write(req)
                        except ValueError:
                            req.cache_forced_bypass = True
                            self.ftl.handle_new_req(req)
                            resumed_reqs = self.cache_manager.discard_unready_registration_for_request(req)
                            for resumed_req in resumed_reqs:
                                if resumed_req.is_serviced():
                                    self._complete_request(resumed_req)
                                else:
                                    self.ftl.handle_new_req(resumed_req)
                            return
                        else:
                            self._complete_request(req)
                            for resumed_req in resumed_reqs:
                                if resumed_req.is_serviced():
                                    self._complete_request(resumed_req)
                                else:
                                    self.ftl.handle_new_req(resumed_req)
                else:
                    self.ftl.tsu.Prepare_trans_submission()
                    self.ftl.tsu.Schedule()
            else:
                raise ValueError(f"Unexpected message type for HIL: {message.type}")
        except RequestFailure as exc:
            if req is None:
                raise
            self._fail_request(req, str(exc))

    def _tile_data(self, req, data):
        if not QUIET:
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

    @staticmethod
    def user_entry_resident_line_count(entry: dict) -> int:
        return sum(1 for ready in entry.get("ready_bitmap", []) if ready)

    @staticmethod
    def static_entry_resident_line_count(entry: dict) -> int:
        return 1 if entry.get("ready") else 0

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
        used_user_lines = sum(
            self.user_entry_resident_line_count(entry)
            for entry in self.user_entries.values()
        )
        used_static_lines = sum(
            self.static_entry_resident_line_count(entry)
            for entry in self.static_entries.values()
        )
        return self._max_lines - used_user_lines - used_static_lines

    def clear(self):
        self.user_entries.clear()
        self.static_entries.clear()


class Cache_Manager:
    def __init__(self, hil: HIL, data_cache_capacity: int | None = None):
        self.hil = hil
        self.cache = Data_Cache(
            capacity=DATA_CACHE_CAP if data_cache_capacity is None else data_cache_capacity
        )
        self.waiting_user_reads: dict[int, list[Request]] = defaultdict(list)
        self.waiting_read_lpas_by_req: dict[str, set[int]] = {}

    @property
    def pending_user_pages(self):
        return self.cache.user_entries

    @property
    def pending_static_pages(self):
        return self.cache.static_entries

    def has_pending_entries(self) -> bool:
        return bool(self.cache.user_entries or self.cache.static_entries)

    def has_flushable_entries(self) -> bool:
        return any(self._user_entry_is_flushable(entry) for entry in self.cache.user_entries.values()) or any(
            entry["ready"] for entry in self.cache.static_entries.values()
        )

    def _line_addr_of_static(self, tr: Transaction) -> int:
        return tr.lpa

    def _new_user_entry(self, sq_id: int) -> dict:
        return {
            "sq_id": sq_id,
            "bitmap": [0] * SECTOR_PER_PAGE,
            "ready_bitmap": [0] * SECTOR_PER_PAGE,
            "payload": [INVALID_DATA] * SECTOR_PER_PAGE,
            "origin_request_ids": [None] * SECTOR_PER_PAGE,
            "generation": 0,
            "flush_inflight_generation": None,
        }

    def _new_static_entry(self, sq_id: int, tr: Transaction) -> dict:
        return {
            "sq_id": sq_id,
            "lpa": tr.lpa,
            "address": tr.address,
            "bitmap": [1],
            "ready": False,
            "payload": [INVALID_DATA],
            "origin_request_ids": [None],
        }

    def _user_entry_covers_transaction(self, entry: dict, tr: Transaction) -> bool:
        for i in range(SECTOR_PER_PAGE):
            if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                continue
            if entry["bitmap"][i] == 0:
                return False
        return True

    def _user_entry_ready_for_transaction(self, entry: dict, tr: Transaction) -> bool:
        for i in range(SECTOR_PER_PAGE):
            if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                continue
            if entry["bitmap"][i] == 0 or entry["ready_bitmap"][i] == 0:
                return False
        return True

    def _user_entry_is_fully_ready(self, entry: dict) -> bool:
        for i in range(SECTOR_PER_PAGE):
            if entry["bitmap"][i] == 1 and entry["ready_bitmap"][i] == 0:
                return False
        return True

    def _user_entry_is_flushable(self, entry: dict) -> bool:
        return (
            self._user_entry_is_fully_ready(entry)
            and entry["flush_inflight_generation"] is None
        )

    def _wait_key(self, req: Request) -> str:
        return req.report_req_id or f"anon-{id(req)}"

    def _unregister_waiting_read(self, req: Request) -> None:
        wait_key = self._wait_key(req)
        blocked_lpas = self.waiting_read_lpas_by_req.pop(wait_key, set())
        for lpa in blocked_lpas:
            retained = [waiting_req for waiting_req in self.waiting_user_reads[lpa] if waiting_req is not req]
            if retained:
                self.waiting_user_reads[lpa] = retained
            else:
                self.waiting_user_reads.pop(lpa, None)

    def _register_waiting_read(self, req: Request, blocked_lpas: set[int]) -> None:
        self._unregister_waiting_read(req)
        if not blocked_lpas:
            return
        wait_key = self._wait_key(req)
        self.waiting_read_lpas_by_req[wait_key] = set(blocked_lpas)
        for lpa in blocked_lpas:
            self.waiting_user_reads[lpa].append(req)

    def _resume_waiting_reads_for_lpas(self, lpas: list[int]) -> list[Request]:
        resumed: dict[str, Request] = {}
        for lpa in set(lpas):
            for req in self.waiting_user_reads.get(lpa, []):
                resumed[self._wait_key(req)] = req
        if not resumed:
            return []
        ready_to_resume: list[Request] = []
        for req in resumed.values():
            self._unregister_waiting_read(req)
            blocked = self.query_cache(req)
            if not blocked:
                ready_to_resume.append(req)
        return ready_to_resume

    def discard_unready_registration_for_request(self, req: Request) -> list[Request]:
        affected_lpas: list[int] = []
        if req.type != RequestType.WRITE:
            return []
        for tr in req.transaction_list:
            entry = self.cache.user_entries.get(tr.lpa)
            if entry is None:
                continue
            changed = False
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                if (
                    entry["origin_request_ids"][i] == req.report_req_id
                    and entry["ready_bitmap"][i] == 0
                ):
                    entry["bitmap"][i] = 0
                    entry["payload"][i] = INVALID_DATA
                    entry["origin_request_ids"][i] = None
                    changed = True
            if changed:
                affected_lpas.append(tr.lpa)
            if (
                not any(entry["bitmap"])
                and not any(entry["ready_bitmap"])
                and entry["flush_inflight_generation"] is None
            ):
                self.cache.user_entries.pop(tr.lpa, None)
        return self._resume_waiting_reads_for_lpas(affected_lpas)

    def _count_new_ready_lines(self, req: Request) -> int:
        if req.type == RequestType.WRITE:
            count = 0
            for tr in req.transaction_list:
                entry = self.cache.user_entries.get(tr.lpa)
                for i in range(SECTOR_PER_PAGE):
                    if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                        continue
                    if entry is None or entry["ready_bitmap"][i] == 0:
                        count += 1
            return count

        if req.type == RequestType.STATIC_WRITE:
            new_ready_lines = 0
            for tr in req.transaction_list:
                line_addr = self._line_addr_of_static(tr)
                entry = self.cache.static_entries.get(line_addr)
                if entry is None or self.cache.static_entry_resident_line_count(entry) == 0:
                    new_ready_lines += 1
            return new_ready_lines

        return 0

    def _ensure_space_for_new_lines(self, new_lines: int) -> None:
        if new_lines <= 0:
            return
        if new_lines > self.cache._max_lines:
            raise ValueError("Incoming write data exceeds DATA_CACHE_CAP")
        if new_lines <= self.cache.free_lines():
            return
        self.write_flush()
        if new_lines > self.cache.free_lines():
            raise ValueError("Incoming write data exceeds DATA_CACHE_CAP")

    def _ensure_cache_not_overfull(self) -> None:
        if self.cache.free_lines() >= 0:
            return
        self.write_flush()
        if self.cache.free_lines() < 0:
            raise ValueError("Incoming write data exceeds DATA_CACHE_CAP")

    def register_write_request(self, req: Request):
        if req.cache_registration_complete:
            return
        self._ensure_space_for_new_lines(self._count_new_ready_lines(req))
        if req.type == RequestType.WRITE:
            self._register_user_write(req)
        elif req.type == RequestType.STATIC_WRITE:
            self._register_static_write(req)
        self._ensure_cache_not_overfull()
        req.cache_registration_complete = True

    def query_cache(self, req: Request) -> bool:
        recorder = REQUEST_LATENCY_RECORDER()
        misses = []
        blocked_lpas: set[int] = set()
        hit_count = 0
        miss_count = 0
        blocked_count = 0
        for tr in req.transaction_list:
            if tr.type != TransactionType.USER_READ:
                misses.append(tr)
                miss_count += 1
                continue
            entry = self.cache.user_entries.get(tr.lpa)
            if entry is None or not self._user_entry_covers_transaction(entry, tr):
                misses.append(tr)
                miss_count += 1
                continue
            if not self._user_entry_ready_for_transaction(entry, tr):
                misses.append(tr)
                blocked_lpas.add(tr.lpa)
                blocked_count += 1
                continue
            payload = [INVALID_DATA] * SECTOR_PER_PAGE
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                if entry["ready_bitmap"][i] == 1:
                    payload[i] = entry["payload"][i]
            tr.payload = payload
            tr.completed = True
            hit_count += 1
        req.transaction_list = misses
        self._register_waiting_read(req, blocked_lpas)
        if recorder is not None:
            recorder.note_data_cache_result(
                req,
                hit_count=hit_count,
                miss_count=miss_count,
                blocked_count=blocked_count,
            )
        return bool(blocked_lpas)

    def cache_write(self, req: Request) -> list[Request]:
        if req.type not in (RequestType.WRITE, RequestType.STATIC_WRITE):
            return []
        # Count new lines BEFORE registering so we know the demand accurately.
        new_lines = self._count_new_ready_lines(req)
        self._ensure_space_for_new_lines(new_lines)
        # Now register (creates entries so write_flush has something to flush).
        self.register_write_request(req)
        if req.type == RequestType.WRITE:
            resumed = self._hydrate_user_write(req)
            self._ensure_cache_not_overfull()
            return resumed
        else:
            self._hydrate_static_write(req)
            self._ensure_cache_not_overfull()
            return []

    def _register_user_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            entry = self.cache.user_entries.setdefault(tr.lpa, self._new_user_entry(sq_id))
            entry["sq_id"] = sq_id
            entry["generation"] += 1
            for i in range(SECTOR_PER_PAGE):
                if i >= len(tr.bitmap) or tr.bitmap[i] == 0:
                    continue
                entry["bitmap"][i] = 1
                entry["ready_bitmap"][i] = 0
                entry["payload"][i] = INVALID_DATA
                entry["origin_request_ids"][i] = req.report_req_id

    def _hydrate_user_write(self, req: Request) -> list[Request]:
        sq_id = req.sq_id if req.sq_id is not None else 0
        hydrated_lpas: list[int] = []
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
                entry["origin_request_ids"][i] = req.report_req_id
            hydrated_lpas.append(tr.lpa)
        return self._resume_waiting_reads_for_lpas(hydrated_lpas)

    def _register_static_write(self, req: Request):
        sq_id = req.sq_id if req.sq_id is not None else 0
        for tr in req.transaction_list:
            line_addr = self._line_addr_of_static(tr)
            entry = self._new_static_entry(sq_id, tr)
            entry["origin_request_ids"] = [req.report_req_id]
            self.cache.static_entries[line_addr] = entry

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
            entry["origin_request_ids"] = [req.report_req_id]

    def write_flush(self):
        flushable_user_pages = {
            lpa: entry
            for lpa, entry in self.cache.user_entries.items()
            if self._user_entry_is_flushable(entry)
        }
        flushable_static_pages = {
            line_addr: entry
            for line_addr, entry in self.cache.static_entries.items()
            if entry["ready"]
        }
        if not flushable_user_pages and not flushable_static_pages:
            return False

        amu = self.hil.ftl.address_mapping_unit
        tsu = self.hil.ftl.tsu

        user_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        user_flush_transactions: list[Transaction] = []
        flushed_user_count = 0
        for lpa, entry in flushable_user_pages.items():
            origin_ids = sorted({req_id for req_id in entry["origin_request_ids"] if req_id})
            generation = entry["generation"]
            tr = Transaction(
                source_req=None,
                type=TransactionType.USER_WRITE,
                lpa=lpa,
                bitmap=list(entry["bitmap"]),
                payload=list(entry["payload"]),
                data_ready=True,
                cache_flush_generated=True,
                cache_flush_generation=generation,
                report_origin_request_ids=origin_ids,
            )
            entry["flush_inflight_generation"] = generation
            user_flush_reqs[entry["sq_id"]].append(tr)
            user_flush_transactions.append(tr)
            flushed_user_count += 1
        if flushed_user_count > 0:
            tsu.start_cache_pressure_drain(flushed_user_count)
        try:
            for sq_id, transaction_list in user_flush_reqs.items():
                origin_ids = sorted(
                    {
                        req_id
                        for tr in transaction_list
                        for req_id in tr.report_origin_request_ids
                        if req_id
                    }
                )
                amu.translate_and_submit(
                    Request(
                        type=RequestType.WRITE,
                        sq_id=sq_id,
                        transaction_list=transaction_list,
                        report_origin_request_ids=origin_ids,
                    )
                )
        except Exception:
            for tr in user_flush_transactions:
                self._release_user_flush_entry(tr, submitted=False)
                tsu.finish_cache_pressure_write()
            raise

        waiting_transaction_ids: set[int] = set()
        block_manager = getattr(self.hil.ftl, "block_manager", None)
        if block_manager is not None:
            for plane_waiting in block_manager.waiting_writes.values():
                for tr in plane_waiting:
                    waiting_transaction_ids.add(id(tr))
        for tr in user_flush_transactions:
            if id(tr) not in waiting_transaction_ids:
                self._release_user_flush_entry(tr, submitted=True)

        static_flush_reqs: dict[int, list[Transaction]] = defaultdict(list)
        for entry in flushable_static_pages.values():
            origin_ids = sorted({req_id for req_id in entry["origin_request_ids"] if req_id})
            static_flush_reqs[entry["sq_id"]].append(
                Transaction(
                    source_req=None,
                    type=TransactionType.USER_STATIC_WRITE,
                    lpa=entry["lpa"],
                    address=entry["address"],
                    bitmap=list(entry["bitmap"]),
                    payload=list(entry["payload"]),
                    data_ready=True,
                    cache_flush_generated=True,
                    report_origin_request_ids=origin_ids,
                )
            )
        for sq_id, transaction_list in static_flush_reqs.items():
            origin_ids = sorted(
                {
                    req_id
                    for tr in transaction_list
                    for req_id in tr.report_origin_request_ids
                    if req_id
                }
            )
            amu.translate_and_submit(
                Request(
                    type=RequestType.STATIC_WRITE,
                    sq_id=sq_id,
                    transaction_list=transaction_list,
                    report_origin_request_ids=origin_ids,
                )
            )
        for line_addr in flushable_static_pages:
            self.cache.static_entries.pop(line_addr, None)
        return True

    def _release_user_flush_entry(self, tr: Transaction, *, submitted: bool) -> None:
        if tr.type != TransactionType.USER_WRITE or not tr.cache_flush_generated:
            return
        entry = self.cache.user_entries.get(tr.lpa)
        if entry is None:
            return
        generation = tr.cache_flush_generation
        if entry["flush_inflight_generation"] != generation:
            return
        entry["flush_inflight_generation"] = None
        if submitted and entry["generation"] == generation:
            self.cache.user_entries.pop(tr.lpa, None)

    def on_waiting_flush_submitted(self, tr: Transaction) -> None:
        self._release_user_flush_entry(tr, submitted=True)

    def on_transaction_serviced(self, tr: Transaction):
        if tr.type in (TransactionType.USER_WRITE, TransactionType.USER_STATIC_WRITE) and tr.cache_flush_generated:
            self.hil.ftl.tsu.finish_cache_pressure_write()
