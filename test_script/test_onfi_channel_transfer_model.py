import pytest

import flash_sim.PHY as phy_module
from flash_sim.config import OnfiTimingConfig
from flash_sim.common import (
    ChipStatus,
    EventType,
    FlashAddress,
    Request,
    RequestType,
    SimEvent,
    Transaction,
    TransactionType,
)
from flash_sim.request_latency_report import RequestLatencyRecorder


def _make_transaction(transaction_type, *, chip=0, source_req=None):
    return Transaction(
        source_req=source_req,
        type=transaction_type,
        address=FlashAddress(channel=0, chip=chip, die=0, plane=0, sub_plane=0, page=0),
        bitmap=[1] * 64,
    )


def _install_event_hooks(monkeypatch, recorder=None):
    state = {"now": 0}
    events = []

    def register_event(event_type, target, param, scheduled_time):
        event = SimEvent(type=event_type, target=target, time=scheduled_time, param=param)
        events.append(event)
        return event

    monkeypatch.setattr(phy_module, "CURRENT_TIME", lambda: state["now"])
    monkeypatch.setattr(phy_module, "Register_event", register_event)
    monkeypatch.setattr(phy_module, "REQUEST_LATENCY_RECORDER", lambda: recorder)
    return state, events


def _prime_read_data_out(phy, transaction):
    chip_id = (0, transaction.address.chip)
    chip_bke = phy.get_chip_bke(chip_id)
    die_bke = chip_bke.get_die_bke(0)
    die_bke.active_command = phy_module.ActiveCommandInfo("read", [transaction])
    chip_bke.status = ChipStatus.READ
    chip_bke.No_of_active_dies = 1
    chip_bke._has_data_waiting = True
    phy._transfer_data(chip_id, 0, "read", [transaction])


def _prime_data_in(phy, transaction, op_kind="write"):
    chip_id = (0, transaction.address.chip)
    phy._enqueue_data_in_transfer(chip_id, 0, op_kind, [transaction], defer_start=False)


def test_nvddr2_timing_helpers_scale_with_payload_plane_count_and_width():
    timing = OnfiTimingConfig(channel_width_bytes=8)

    small_in = phy_module.onfi_data_in_duration(1024, timing)
    large_in = phy_module.onfi_data_in_duration(2048, timing)
    small_out = phy_module.onfi_data_out_duration(1024, 1, timing)
    large_out = phy_module.onfi_data_out_duration(2048, 1, timing)

    assert small_in > 0
    assert small_out > 0
    assert large_in > small_in
    assert large_out > small_out
    assert phy_module.onfi_read_command_duration(2, timing) > phy_module.onfi_read_command_duration(1, timing)
    assert phy_module.onfi_program_command_duration(2, timing) > phy_module.onfi_program_command_duration(1, timing)
    assert phy_module.onfi_erase_command_duration(2, timing) > phy_module.onfi_erase_command_duration(1, timing)

    narrow = OnfiTimingConfig(channel_width_bytes=4)
    wide = OnfiTimingConfig(channel_width_bytes=16)
    assert phy_module.onfi_data_in_duration(4096, wide) <= phy_module.onfi_data_in_duration(4096, narrow)


def test_channel_scheduler_uses_requested_priority_order(monkeypatch):
    _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    transaction = _make_transaction(TransactionType.USER_READ)
    order = [
        phy_module.ChannelTransferKind.COMMAND,
        phy_module.ChannelTransferKind.USER_DATA_IN,
        phy_module.ChannelTransferKind.GC_WRITE_DATA_IN,
        phy_module.ChannelTransferKind.MAPPING_DATA_OUT,
        phy_module.ChannelTransferKind.USER_DATA_OUT,
        phy_module.ChannelTransferKind.STATIC_RESULT_DATA_OUT,
        phy_module.ChannelTransferKind.GC_READ_DATA_OUT,
    ]
    shuffled = [
        phy_module.ChannelTransferKind.GC_READ_DATA_OUT,
        phy_module.ChannelTransferKind.USER_DATA_OUT,
        phy_module.ChannelTransferKind.GC_WRITE_DATA_IN,
        phy_module.ChannelTransferKind.COMMAND,
        phy_module.ChannelTransferKind.STATIC_RESULT_DATA_OUT,
        phy_module.ChannelTransferKind.MAPPING_DATA_OUT,
        phy_module.ChannelTransferKind.USER_DATA_IN,
    ]

    for kind in shuffled:
        op_kind = "write" if kind in phy_module.DATA_IN_TRANSFER_KINDS else "read"
        phy._submit_channel_transfer(
            phy_module.ChannelTransferTask(
                kind=kind,
                channel_id=0,
                chip_id=(0, 0),
                die_id=0,
                transactions=[transaction],
                total_duration=10,
                op_kind=op_kind,
            ),
            defer_start=True,
        )

    observed = []
    while phy._pending_transfers[0]:
        phy.schedule_next_channel_transfer(0)
        observed.append(phy._active_transfers[0].kind)
        phy._active_transfers[0] = None
        phy._channel_busy[0] = False

    assert observed == order


def test_command_preempts_active_user_data_out_and_resumes_remaining_duration(monkeypatch):
    state, events = _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    data_transaction = _make_transaction(TransactionType.USER_READ, chip=0)
    _prime_read_data_out(phy, data_transaction)

    old_data_out_event = events[-1]
    old_finish = old_data_out_event.time
    state["now"] = 100
    command_transaction = _make_transaction(TransactionType.USER_READ, chip=1)

    phy.send_command_to_chip((0, 1), [command_transaction], False)

    assert old_data_out_event.ignored is True
    assert data_transaction.completed is False
    remaining = old_finish - state["now"]
    assert remaining > 0
    assert phy._active_transfers[0].kind is phy_module.ChannelTransferKind.COMMAND

    stale_event_count = len(events)
    state["now"] = old_data_out_event.time
    phy.execute(old_data_out_event)
    assert len(events) == stale_event_count
    assert data_transaction.completed is False

    command_event = phy._active_transfers[0].completion_event
    state["now"] = command_event.time
    phy.execute(command_event)

    resumed_event = phy._active_transfers[0].completion_event
    assert resumed_event is not old_data_out_event
    assert resumed_event.time == state["now"] + remaining

    state["now"] = resumed_event.time
    phy.execute(resumed_event)
    assert data_transaction.completed is True


@pytest.mark.parametrize(
    ("transaction_type", "cmd_type", "expected_kind"),
    [
        (TransactionType.MAPPING_READ, "read", phy_module.ChannelTransferKind.MAPPING_DATA_OUT),
        (TransactionType.USER_READ, "read", phy_module.ChannelTransferKind.USER_DATA_OUT),
        (TransactionType.USER_SEARCH, "search", phy_module.ChannelTransferKind.STATIC_RESULT_DATA_OUT),
        (TransactionType.USER_COMPUTE, "compute", phy_module.ChannelTransferKind.STATIC_RESULT_DATA_OUT),
        (TransactionType.GC_READ, "read", phy_module.ChannelTransferKind.GC_READ_DATA_OUT),
    ],
)
def test_command_preempts_all_lower_priority_data_out_classes(
    monkeypatch,
    transaction_type,
    cmd_type,
    expected_kind,
):
    state, events = _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    data_transaction = _make_transaction(transaction_type, chip=0)
    phy._enqueue_data_out_transfer((0, 0), 0, cmd_type, [data_transaction], defer_start=False)

    old_event = events[-1]
    state["now"] = 100
    command_transaction = _make_transaction(TransactionType.USER_READ, chip=1)

    phy.send_command_to_chip((0, 1), [command_transaction], False)

    assert old_event.ignored is True
    assert phy._active_transfers[0].kind is phy_module.ChannelTransferKind.COMMAND
    assert any(task.kind is expected_kind for task in phy._pending_transfers[0])


def test_command_preempts_active_data_in_and_resumes_remaining_duration(monkeypatch):
    state, events = _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    data_transaction = _make_transaction(TransactionType.USER_WRITE, chip=0)
    _prime_data_in(phy, data_transaction)

    old_data_in_event = events[-1]
    old_finish = old_data_in_event.time
    state["now"] = 100
    command_transaction = _make_transaction(TransactionType.USER_READ, chip=1)

    phy.send_command_to_chip((0, 1), [command_transaction], False)

    assert old_data_in_event.ignored is True
    remaining = old_finish - state["now"]
    assert remaining > 0
    assert phy._active_transfers[0].kind is phy_module.ChannelTransferKind.COMMAND

    stale_event_count = len(events)
    state["now"] = old_data_in_event.time
    phy.execute(old_data_in_event)
    assert len(events) == stale_event_count

    command_event = phy._active_transfers[0].completion_event
    state["now"] = command_event.time
    phy.execute(command_event)

    resumed_event = phy._active_transfers[0].completion_event
    assert resumed_event is not old_data_in_event
    assert resumed_event.time == state["now"] + remaining


def test_command_preempts_compute_result_and_resumes_exact_remaining_duration(monkeypatch):
    state, events = _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    compute_req = Request(type=RequestType.COMPUTE, selected_wl=0)
    transaction = _make_transaction(
        TransactionType.USER_COMPUTE, chip=0, source_req=compute_req
    )
    task = phy._enqueue_data_out_transfer(
        (0, 0), 0, "compute", [transaction], defer_start=False
    )
    original_event = events[-1]
    original_duration = task.total_duration

    state["now"] = 100
    command = _make_transaction(TransactionType.USER_READ, chip=1)
    phy.send_command_to_chip((0, 1), [command], False)

    assert original_event.ignored is True
    assert task.payload_bytes == 262_144
    assert task.remaining_duration == original_duration - 100
    assert phy._active_transfers[0].kind is phy_module.ChannelTransferKind.COMMAND

    command_event = phy._active_transfers[0].completion_event
    state["now"] = command_event.time
    phy.execute(command_event)

    resumed_event = phy._active_transfers[0].completion_event
    assert resumed_event.time - state["now"] == original_duration - 100
    state["now"] = resumed_event.time
    phy.execute(resumed_event)

    assert transaction.completed is True
    assert task.payload_bytes == 262_144


def test_active_command_is_not_preempted_by_another_command(monkeypatch):
    state, events = _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    first_command = _make_transaction(TransactionType.USER_READ, chip=0)
    second_command = _make_transaction(TransactionType.USER_READ, chip=1)

    phy.send_command_to_chip((0, 0), [first_command], False)
    old_command_event = events[-1]
    state["now"] = 10
    phy.send_command_to_chip((0, 1), [second_command], False)

    assert old_command_event.ignored is False
    assert phy._active_transfers[0].transactions == [first_command]
    assert any(task.transactions == [second_command] for task in phy._pending_transfers[0])

    state["now"] = old_command_event.time
    phy.execute(old_command_event)

    assert phy._active_transfers[0].transactions == [second_command]


def test_request_latency_report_splits_preempted_data_out_intervals(monkeypatch):
    recorder = RequestLatencyRecorder()
    state, _ = _install_event_hooks(monkeypatch, recorder)
    phy = phy_module.PHY()

    read_req = Request(type=RequestType.READ, lha_start=0, size=64, report_req_id="read-req")
    command_req = Request(type=RequestType.READ, lha_start=64, size=64, report_req_id="cmd-req")
    recorder.register_request(read_req, scheduled_time=0)
    recorder.register_request(command_req, scheduled_time=0)

    data_transaction = _make_transaction(TransactionType.USER_READ, chip=0, source_req=read_req)
    _prime_read_data_out(phy, data_transaction)
    old_data_out_event = phy._active_transfers[0].completion_event

    state["now"] = 100
    command_transaction = _make_transaction(TransactionType.USER_READ, chip=1, source_req=command_req)
    phy.send_command_to_chip((0, 1), [command_transaction], False)

    command_event = phy._active_transfers[0].completion_event
    state["now"] = command_event.time
    phy.execute(command_event)

    resumed_event = phy._active_transfers[0].completion_event
    state["now"] = resumed_event.time
    phy.execute(resumed_event)

    read_intervals = recorder.requests[read_req.report_req_id].intervals["phy_data_out"]
    command_intervals = recorder.requests[command_req.report_req_id].intervals["phy_cmd_addr"]

    assert old_data_out_event.ignored is True
    assert len(read_intervals) == 2
    assert read_intervals[0]["start"] == 0
    assert read_intervals[0]["end"] == 100
    assert read_intervals[1]["start"] == command_intervals[0]["end"]
    assert read_intervals[0]["end"] <= command_intervals[0]["start"]
    assert command_intervals[0]["end"] <= read_intervals[1]["start"]


def test_request_latency_report_splits_preempted_data_in_intervals(monkeypatch):
    recorder = RequestLatencyRecorder()
    state, _ = _install_event_hooks(monkeypatch, recorder)
    phy = phy_module.PHY()

    write_req = Request(type=RequestType.WRITE, lha_start=0, size=64, report_req_id="write-req")
    command_req = Request(type=RequestType.READ, lha_start=64, size=64, report_req_id="cmd-req")
    recorder.register_request(write_req, scheduled_time=0)
    recorder.register_request(command_req, scheduled_time=0)

    data_transaction = _make_transaction(TransactionType.USER_WRITE, chip=0, source_req=write_req)
    _prime_data_in(phy, data_transaction)
    old_data_in_event = phy._active_transfers[0].completion_event

    state["now"] = 100
    command_transaction = _make_transaction(TransactionType.USER_READ, chip=1, source_req=command_req)
    phy.send_command_to_chip((0, 1), [command_transaction], False)

    command_event = phy._active_transfers[0].completion_event
    state["now"] = command_event.time
    phy.execute(command_event)

    resumed_event = phy._active_transfers[0].completion_event
    state["now"] = resumed_event.time
    phy.execute(resumed_event)

    data_in_intervals = recorder.requests[write_req.report_req_id].intervals["phy_data_in"]
    command_intervals = recorder.requests[command_req.report_req_id].intervals["phy_cmd_addr"]

    assert old_data_in_event.ignored is True
    assert len(data_in_intervals) == 2
    assert data_in_intervals[0]["start"] == 0
    assert data_in_intervals[0]["end"] == 100
    assert data_in_intervals[1]["start"] == command_intervals[0]["end"]
    assert data_in_intervals[0]["end"] <= command_intervals[0]["start"]
    assert command_intervals[0]["end"] <= data_in_intervals[1]["start"]
    assert sum(interval["end"] - interval["start"] for interval in data_in_intervals) == old_data_in_event.time


def test_search_payload_has_one_wl_input_and_concat_output_per_plane():
    phy = phy_module.PHY()
    req = Request(type=RequestType.SEARCH)
    transactions = [
        _make_transaction(TransactionType.USER_SEARCH, source_req=req),
        _make_transaction(TransactionType.USER_SEARCH, source_req=req),
        _make_transaction(TransactionType.USER_SEARCH, source_req=req),
    ]
    transactions[1].address.plane = 1
    transactions[2].address.plane = 1

    assert phy._data_in_payload_bytes("search", transactions) == 16
    assert phy._data_out_payload_bytes("search", transactions) == 65_536


def test_compute_payload_has_one_input_per_transaction_and_adc_output_per_plane():
    phy = phy_module.PHY()
    req = Request(type=RequestType.COMPUTE, selected_wl=0)
    transactions = [
        _make_transaction(TransactionType.USER_COMPUTE, source_req=req),
        _make_transaction(TransactionType.USER_COMPUTE, source_req=req),
        _make_transaction(TransactionType.USER_COMPUTE, source_req=req),
    ]
    transactions[2].address.plane = 1

    assert phy._data_in_payload_bytes("compute", transactions) == 3
    assert phy._data_out_payload_bytes("compute", transactions) == 524_288


def test_cim_payload_rounds_non_byte_aligned_configurations_up():
    geometry = type(
        "Geometry",
        (),
        {
            "wl_per_string": 3,
            "search_input_bits_per_wl": 3,
            "bl_per_plane": 5,
            "search_match_bits_per_bl": 3,
            "compute_input_bits_per_sl": 3,
            "compute_accumulator_bits": 5,
        },
    )()
    phy = phy_module.PHY(cim_geometry=geometry)
    req = Request(type=RequestType.COMPUTE, selected_wl=0)
    transactions = [
        _make_transaction(TransactionType.USER_COMPUTE, source_req=req)
        for _ in range(3)
    ]
    assert phy._data_in_payload_bytes("search", transactions) == 2
    assert phy._data_out_payload_bytes("search", transactions) == 2
    assert phy._data_in_payload_bytes("compute", transactions) == 2
    assert phy._data_out_payload_bytes("compute", transactions) == 4


def test_channel_task_freezes_cim_payload_and_duration_when_wave_is_created(monkeypatch):
    _install_event_hooks(monkeypatch)
    phy = phy_module.PHY()
    req = Request(type=RequestType.COMPUTE, selected_wl=0)
    transaction = _make_transaction(TransactionType.USER_COMPUTE, source_req=req)

    task = phy._enqueue_data_out_transfer(
        (0, 0), 0, "compute", [transaction], defer_start=True
    )
    original_duration = task.total_duration
    phy.compute_accumulator_bits = 4

    assert task.payload_bytes == 262_144
    assert task.total_duration == original_duration
    assert phy._data_out_payload_bytes("compute", [transaction]) == 131_072


def test_read_and_write_still_use_transaction_sector_payloads():
    phy = phy_module.PHY()
    read = _make_transaction(TransactionType.USER_READ)
    write = _make_transaction(TransactionType.USER_WRITE)

    assert phy._data_out_payload_bytes("read", [read]) == phy._transfer_payload_bytes([read])
    assert phy._data_in_payload_bytes("write", [write]) == phy._transfer_payload_bytes([write])
