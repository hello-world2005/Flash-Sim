# -*- coding: utf-8 -*-
from queue import Queue

from .common import *
from .pcie_link import PCIe_link, PCIe_message
from .common import CQ_ENTRY_SIZE_BASIC

class CQ_Entry:
    def __init__(self, source_req, timestamp):
        self.source_req = source_req
        self.size = CQ_ENTRY_SIZE_BASIC
        self.timestamp = timestamp

class Host:

    class Memory:
        def __init__(self, queue_ptrs=None, num_of_queues=8, depth=64):
            self.storage = {}
            self.sq_entries = [[] for _ in range(num_of_queues)]
            self._queue_ptrs = queue_ptrs
            self._depth = depth

        def read(self, address: int, size: int) -> bytes:
            # if address == VIRTUAL_DATA_ADDRESS:
            #     return b'\x00' * size
            # return self.storage[address]
            return [11 for _ in range(size)]

        def write(self, address: int, data: bytes):
            self.storage[address] = data

        def sq_push(self, queue_id, req):
            self.sq_entries[queue_id].append(req)
            if self._queue_ptrs is not None:
                self._queue_ptrs.sq_tails[queue_id] = (
                    self._queue_ptrs.sq_tails[queue_id] + 1
                ) % self._depth

        def get_req_data(self, req):
            if req.type == RequestType.WRITE:
                return self.read(req.data_address, req.data_size)
            elif req.type == RequestType.SEARCH:
                return self.read(req.data_address, req.data_size)
            elif req.type == RequestType.COMPUTE:
                return self.read(req.data_address, req.data_size)
            else:
                raise ValueError(f"{req.type} request has no data attached!")

    class Queue_ptrs:
        def __init__(self, num_of_queues, depth_of_queues):
            self.sq_heads = [0] * num_of_queues
            self.sq_tails = [0] * num_of_queues
            self.cq_heads = [0] * num_of_queues
            self.cq_tails = [0] * num_of_queues
            self.depth = depth_of_queues
            self.num_of_queues = num_of_queues

        def is_sq_empty(self, queue_id):
            return self.sq_heads[queue_id] == self.sq_tails[queue_id]

        def is_sq_full(self, queue_id):
            return (self.sq_tails[queue_id] + 1) % self.depth == self.sq_heads[queue_id]

        def find_available_sq(self):
            for i in range(self.num_of_queues):
                if not self.is_sq_full(i):
                    return i
            return None

    class IO_Flow:
        def __init__(self, sq_id=None):
            self.busy = False
            self.current_req = None
            self.sq_id = sq_id

    class IO_Flow_Manager:
        def __init__(self, flows, queue_ptrs):
            self.io_flows = flows
            self.queue_ptrs = queue_ptrs

        def find_available_flow(self):
            for flow in self.io_flows:
                if not flow.busy:
                    return flow
            return None
        
        def is_flow_available(self, sq_id):
            return not self.io_flows[sq_id].busy

        def get_sq_for_request(self, req):
            min_occupancy = float('inf')
            best_sq_id = None
            for sq_id in range(self.queue_ptrs.num_of_queues):
                if not self.queue_ptrs.is_sq_full(sq_id):
                    occupancy = self.queue_ptrs.sq_tails[sq_id] % self.queue_ptrs.depth - self.queue_ptrs.sq_heads[sq_id] % self.queue_ptrs.depth
                    if occupancy < min_occupancy:
                        min_occupancy = occupancy
                        best_sq_id = sq_id
            return best_sq_id

    def __init__(self, name="Host", num_of_queues=8, depth_of_queues=64):
        print("Initializing Host...")
        self.name = name
        self.num_of_queues = num_of_queues
        self.queue_ptrs = self.Queue_ptrs(num_of_queues, depth_of_queues)
        self.memory = self.Memory(
            queue_ptrs=self.queue_ptrs,
            num_of_queues=num_of_queues,
            depth=depth_of_queues,
        )
        self.pcie_link: PCIe_link
        self.io_flows = [self.IO_Flow(sq_id=i) for i in range(num_of_queues)]
        self.io_flow_manager = self.IO_Flow_Manager(self.io_flows, self.queue_ptrs)
        self.waiting_req = Queue()
        self._construction_valid: bool = False
        print("Host initialization complete.")

    def Validate_construction(self):
        if self._construction_valid:
            return
        print("Validating Host construction...")
        assert self.pcie_link is not None, "PCIe link is not set for Host"
        assert self.io_flow_manager is not None, "IO flow manager is not set for Host"
        assert self.waiting_req is not None, "Waiting request queue is not set for Host"
        assert self.memory is not None, "Memory is not set for Host"
        assert self.queue_ptrs is not None, "Queue pointers are not set for Host"
        assert self.io_flows is not None, "IO flows are not set for Host"
        self._construction_valid = True
        print("Host construction validation complete.")

    def execute(self, event):
        log_execute_event(self.__class__.__name__, event)
        if event.type == EventType.REQ_INIT:
            assert event.target == self
            req = event.param["req"]
            self.submit_req(req)
        elif event.type == EventType.DELIVER:
            message = event.param["message"]
            if message.type in [MessageType.WRITE_DATA_REQ, MessageType.SEARCH_DATA_REQ, MessageType.COMPUTE_DATA_REQ]:
                self.send_data(message)
            elif message.type in [MessageType.WRITE_DATA_RECEIVED, MessageType.READ_REQ_RECEIVED, MessageType.SEARCH_DATA_RECEIVED, MessageType.COMPUTE_DATA_RECEIVED]:
                self.remove_from_sq(message.payload["sq_id"])
                if not self.queue_ptrs.is_sq_empty(message.payload["sq_id"]):
                    self.send_next_req(message.payload["sq_id"])
            elif message.type in [MessageType.READ_RES_SEND_BACK, MessageType.SEARCH_RES_SEND_BACK, MessageType.COMPUTE_RES_SEND_BACK]:
                self.memory.write(message.payload["address"], message.payload["data"])
            elif message.type == MessageType.REQ_COMP:
                req = message.payload["req"]
                print(f"[Host] Received REQ_COMP:\n{req}")
                req.finish_time = CURRENT_TIME()
                self.consume_cq(req)
            else:
                raise ValueError(f"Invalid message type: {message.type}")
    
    def remove_from_sq(self, sq_id):
        self.queue_ptrs.sq_heads[sq_id] = (
            self.queue_ptrs.sq_heads[sq_id] + 1
        ) % self.queue_ptrs.depth
        self.io_flows[sq_id].busy = False
        self.io_flows[sq_id].current_req = None
        if not self.waiting_req.empty():
            next_req = self.waiting_req.get()
            self.submit_req(next_req)
    

    def send_next_req(self, sq_id):
        next_req = self.memory.sq_pop(sq_id)
        message_type = None
        if next_req.type == RequestType.WRITE:
            message_type = MessageType.WRITE_REQ
        elif next_req.type == RequestType.READ:
            message_type = MessageType.READ_REQ
        elif next_req.type == RequestType.SEARCH:
            message_type = MessageType.SEARCH_REQ
        elif next_req.type == RequestType.COMPUTE:
            message_type = MessageType.COMPUTE_REQ
        else: raise ValueError(f"Invalid request type: {next_req.type}")
        message = PCIe_message(
            type=message_type, payload={"req": next_req}
        )
        self.pcie_link.send(message, self.pcie_link.device)

    def submit_req(self, req):
        target_sq_id = self.io_flow_manager.get_sq_for_request(req) # push req into a not full submission queue
        if target_sq_id is None:
            self.waiting_req.put(req) # if no available submission queue, put req into waiting queue
            return
        self.memory.sq_push(target_sq_id, req)
        req.sq_id = target_sq_id
        req.issue_time = CURRENT_TIME()
        if self.io_flow_manager.is_flow_available(target_sq_id):
            flow = self.io_flow_manager.io_flows[target_sq_id]
            flow.busy = True
            flow.current_req = req
            self.send_req(req)
    
    def send_req(self, req):
        msg_type = None
        if req.type == RequestType.WRITE:
            msg_type = MessageType.WRITE_REQ
        elif req.type == RequestType.READ:
            msg_type = MessageType.READ_REQ
        elif req.type == RequestType.SEARCH:
            msg_type = MessageType.SEARCH_REQ
        elif req.type == RequestType.COMPUTE:
            msg_type = MessageType.COMPUTE_REQ
        else: raise ValueError(f"Invalid request type: {req.type}")
        message = PCIe_message(msg_type, payload={"req": req})
        self.pcie_link.send(message, self.pcie_link.device)


    # def inform_sq_head_update(self, sq_id):
    #     self.pcie_link.send(
    #         PCIe_link.PCIe_message(
    #             type=SQ_INFORM,
    #             payload={
    #                 "sq_id": sq_id,
    #                 "new_head": self.queue_ptrs.sq_heads[sq_id],
    #                 "new_tail": self.queue_ptrs.sq_tails[sq_id],
    #             },
    #             source_req=None,
    #             sq_id=sq_id,
    #         ),
    #         self.pcie_link.device,
    #     )

    # def inform_cq_tail_update(self, cq_id):
    #     self.pcie_link.send(
    #         PCIe_link.PCIe_message(
    #             type=CQ_INFORM,
    #             payload={
    #                 "cq_id": cq_id,
    #                 "new_head": self.queue_ptrs.cq_heads[cq_id],
    #                 "new_tail": self.queue_ptrs.cq_tails[cq_id],
    #             },
    #             source_req=None,
    #             sq_id=cq_id,
    #         ),
    #         self.pcie_link.device,
    #     )

    # 简单起见，当前版本不考虑nvme协议中双边sq, cq指针维护的问题，认为device可以直接读到host的指针

    def consume_cq(self, cqe):
        cq_id = getattr(cqe, "cq_id", 0)
        self.queue_ptrs.cq_tails[cq_id] = (
            self.queue_ptrs.cq_tails[cq_id] + 1
        ) % self.queue_ptrs.depth
        self.inform_cq_tail_update(cq_id)

    def send_data(self, message):
        req = message.payload["req"]
        data = self.memory.get_req_data(req)
        new_message = PCIe_message(
            type=MessageType.WRITE_DATA if req.type == RequestType.WRITE else MessageType.SEARCH_DATA if req.type == RequestType.SEARCH else MessageType.COMPUTE_DATA,
            payload={"req": req, "data": data}
        )
        self.pcie_link.send(new_message, self.pcie_link.device)