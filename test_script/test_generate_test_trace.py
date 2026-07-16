from flash_sim.common import SECTOR_PER_PAGE, STATIC_BASE_LHA

from test_script.generate_test_trace import (
    build_runtime_context,
    generate_trace,
    plane_key_for_random_access_lpa,
)


def test_generate_trace_is_deterministic_for_same_seed():
    first = generate_trace(seed=13, request_budget=12)
    second = generate_trace(seed=13, request_budget=12)

    assert first.commands == second.commands
    assert first.summary == second.summary


def test_generate_trace_contains_all_primary_types_and_legal_domains():
    context = build_runtime_context()
    generated = generate_trace(seed=29, request_budget=12)

    assert {"read", "write", "search", "compute"} <= set(generated.summary.type_counts)

    for command in generated.commands:
        if command["type"] in {"read", "write"}:
            assert 0 <= command["start_lha"] < STATIC_BASE_LHA
            assert command["start_lha"] + command["size"] <= STATIC_BASE_LHA
        else:
            assert context.static_start_lha <= command["start_lha"] < context.static_end_lha
            assert command["start_lha"] + command["size"] <= context.static_end_lha
        if command["type"] == "compute":
            assert command["selected_wl"] == 0


def test_generate_trace_reads_preconditioned_and_freshly_written_data():
    context = build_runtime_context()
    generated = generate_trace(seed=41, request_budget=12)

    valid_plane_keys = {plane.key for plane in context.plane_snapshots}
    valid_precondition_lpas = {
        int(record["lpa"])
        for record in context.precondition_records
        if plane_key_for_random_access_lpa(int(record["lpa"])) in valid_plane_keys
    }

    assert generated.summary.precondition_read_lpas
    assert set(generated.summary.precondition_read_lpas) <= valid_precondition_lpas

    positions = {
        request.request_id: index for index, request in enumerate(generated.ordered_requests)
    }
    write_requests = {
        request.request_id: request
        for request in generated.ordered_requests
        if request.command_type == "write"
    }
    readback_requests = [
        request for request in generated.ordered_requests if request.role == "write-readback"
    ]

    assert readback_requests
    for readback in readback_requests:
        dependency_id = readback.depends_on[0]
        write_request = write_requests[dependency_id]
        assert positions[dependency_id] < positions[readback.request_id]
        assert write_request.start_lha <= readback.start_lha < write_request.start_lha + SECTOR_PER_PAGE
