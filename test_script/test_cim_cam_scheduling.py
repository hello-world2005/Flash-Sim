from types import SimpleNamespace

import pytest

import flash_sim.FTL as ftl_module
from flash_sim.FTL import FTL, TSU
from flash_sim.HIL import HIL
from flash_sim.common import (
    FlashAddress,
    Request,
    RequestType,
    STATIC_BASE_LHA,
    Transaction,
    TransactionType,
)


class _RecordingPHY:
    def __init__(self):
        self.commands = []

    def send_command_to_chip(self, chip_id, transactions, suspension_required):
        self.commands.append((chip_id, list(transactions), suspension_required))


def _make_tsu(monkeypatch):
    monkeypatch.setattr(ftl_module, "REQUEST_LATENCY_RECORDER", lambda: None)
    tsu = TSU.__new__(TSU)
    tsu.phy = _RecordingPHY()
    tsu.compute_max_parallel_sl = ftl_module.COMPUTE_MAX_PARALLEL_SL
    tsu._transaction_blocked_by_barrier = lambda _tr: False
    tsu._mark_dispatched = lambda _chip_id, _count: None
    return tsu


def _transaction(req, transaction_type, *, die=0, plane=0, sub_plane=0):
    return Transaction(
        source_req=req,
        type=transaction_type,
        address=FlashAddress(
            channel=0,
            chip=0,
            die=die,
            plane=plane,
            sub_plane=sub_plane,
            page=-1,
        ),
        data_ready=True,
    )


def test_hil_segments_static_requests_at_ssl_granularity():
    hil = HIL.__new__(HIL)
    hil.ftl = FTL.__new__(FTL)
    req = Request(
        type=RequestType.COMPUTE,
        lha_start=STATIC_BASE_LHA,
        size=5,
        selected_wl=3,
    )

    hil.segment(req)

    assert [tr.address.sub_plane for tr in req.transaction_list] == [0, 1, 2, 3, 4]
    assert [TSU._decode_static_sub_plane(tr.address.sub_plane) for tr in req.transaction_list] == [
        (0, 0, 0),
        (0, 0, 1),
        (0, 0, 2),
        (0, 0, 3),
        (0, 1, 0),
    ]


def test_compute_selects_one_ssl_per_block_sl_but_allows_different_sls(monkeypatch):
    tsu = _make_tsu(monkeypatch)
    req = Request(type=RequestType.COMPUTE, selected_wl=9)
    same_sl_first = _transaction(req, TransactionType.USER_COMPUTE, sub_plane=0)
    same_sl_second = _transaction(req, TransactionType.USER_COMPUTE, sub_plane=1)
    next_sl = _transaction(req, TransactionType.USER_COMPUTE, sub_plane=4)
    q = [same_sl_first, same_sl_second, next_sl]

    assert tsu.issue_compute_command((0, 0), q)

    assert tsu.phy.commands[0][1] == [same_sl_first, next_sl]
    assert q == [same_sl_second]


def test_compute_plane_limit_creates_another_wave(monkeypatch):
    tsu = _make_tsu(monkeypatch)
    tsu.compute_max_parallel_sl = 1
    req = Request(type=RequestType.COMPUTE, selected_wl=0)
    first_sl = _transaction(req, TransactionType.USER_COMPUTE, sub_plane=0)
    second_sl = _transaction(req, TransactionType.USER_COMPUTE, sub_plane=4)
    q = [first_sl, second_sl]

    tsu.issue_compute_command((0, 0), q)

    assert tsu.phy.commands[0][1] == [first_sl]
    assert q == [second_sl]


def test_compute_same_die_requires_same_source_request_and_selected_wl(monkeypatch):
    tsu = _make_tsu(monkeypatch)
    first_req = Request(type=RequestType.COMPUTE, selected_wl=2)
    other_req = Request(type=RequestType.COMPUTE, selected_wl=2)
    first = _transaction(first_req, TransactionType.USER_COMPUTE, plane=0)
    other = _transaction(other_req, TransactionType.USER_COMPUTE, plane=1)
    q = [first, other]

    tsu.issue_compute_command((0, 0), q)

    assert tsu.phy.commands[0][1] == [first]
    assert q == [other]


@pytest.mark.parametrize("transaction_type", [TransactionType.USER_SEARCH, TransactionType.USER_COMPUTE])
def test_search_and_compute_choose_requests_independently_across_dies(monkeypatch, transaction_type):
    tsu = _make_tsu(monkeypatch)
    request_type = (
        RequestType.SEARCH if transaction_type == TransactionType.USER_SEARCH else RequestType.COMPUTE
    )
    first_req = Request(type=request_type, selected_wl=1)
    second_req = Request(type=request_type, selected_wl=7)
    die_zero = _transaction(first_req, transaction_type, die=0)
    die_one = _transaction(second_req, transaction_type, die=1)
    q = [die_zero, die_one]

    issue = (
        tsu.issue_search_command
        if transaction_type == TransactionType.USER_SEARCH
        else tsu.issue_compute_command
    )
    assert issue((0, 0), q)

    assert [command[1] for command in tsu.phy.commands] == [[die_zero], [die_one]]
    assert q == []


def test_search_selects_at_most_one_ssl_per_plane_for_a_die_wave(monkeypatch):
    tsu = _make_tsu(monkeypatch)
    req = Request(type=RequestType.SEARCH)
    plane_zero_first = _transaction(req, TransactionType.USER_SEARCH, plane=0, sub_plane=0)
    plane_zero_second = _transaction(req, TransactionType.USER_SEARCH, plane=0, sub_plane=1)
    plane_one = _transaction(req, TransactionType.USER_SEARCH, plane=1, sub_plane=0)
    q = [plane_zero_first, plane_zero_second, plane_one]

    tsu.issue_search_command((0, 0), q)

    assert tsu.phy.commands[0][1] == [plane_zero_first, plane_one]
    assert q == [plane_zero_second]
