from __future__ import annotations

"""Gateway request execution should surface bad request files as runtime errors."""

from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.io import gateway_response_path, read_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.recovery import recover_gateway_processing_requests_report
from agent_py_agent.agent.gateway_parts.request_execution import (
    _copy_final_lease_fields,
    _handle_gateway_request,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    _finish_claimed_gateway_request,
    _iter_pending_request_paths,
    _process_gateway_requests,
)
from agent_py_agent.agent.settings import AgentConfig


def _make_agent(tmp_path: Path) -> tuple[SimpleAgent, object]:
    cfg = AgentConfig(
        model_backend="echo",
        gateway_workspace="gateway",
        local_store_path="local_store/local.db",
        local_store_files_dir="local_store/files",
        local_store_events_path="local_store/events.jsonl",
    )
    agent = SimpleAgent(cfg, tmp_path)
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return agent, paths


def test_handle_gateway_request_reports_bad_request_json(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-bad-request.json"
    request_path.write_text("{bad json", encoding="utf-8")

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.request_execution.request.read"
    assert "unsupported" not in response["error"].lower()


def test_handle_gateway_request_reports_bad_existing_response_json(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-bad-response"
    request_path = paths.processing / f"{request_id}.json"
    request_path.write_text(
        '{"id": "gw-bad-response", "kind": "ask", "prompt": "should not rerun"}',
        encoding="utf-8",
    )
    gateway_response_path(paths, request_id).write_text("{bad json", encoding="utf-8")

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_RESPONSE_LOAD_ERROR"
    assert response["response_load_error"]["context"] == "gateway.request_execution.response.read"


def test_process_gateway_requests_preserves_bad_request_diagnostic(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.inbox / "gw-bad-inbox.json"
    request_path.write_text("{bad json", encoding="utf-8")

    assert _process_gateway_requests(agent, paths) == 1

    response = read_json_file(gateway_response_path(paths, "gw-bad-inbox"))
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.worker.request.read"
    assert (paths.failed / "gw-bad-inbox.json").exists()
    assert not request_path.exists()


def test_pending_request_iteration_keeps_fresh_requests_ahead_of_recovery(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    (paths.inbox / "normal.json").write_text(
        '{"id": "normal", "priority": "interactive", "created_at": 1}',
        encoding="utf-8",
    )
    (paths.inbox / "recovery.json").write_text(
        '{"id": "recovery", "priority": "recovery", "created_at": 2}',
        encoding="utf-8",
    )

    assert [path.name for path in _iter_pending_request_paths(paths)] == ["normal.json", "recovery.json"]


def test_process_gateway_requests_skips_future_not_before_request(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.inbox / "future.json"
    request_path.write_text(
        '{"id": "future", "kind": "ask", "prompt": "later", "not_before_at": 99999999999}',
        encoding="utf-8",
    )

    assert _process_gateway_requests(agent, paths) == 0
    assert request_path.exists()
    assert not (paths.processing / "future.json").exists()


def test_recover_processing_request_reports_bad_request_json(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-bad-processing.json"
    request_path.write_text("{bad json", encoding="utf-8")

    report = recover_gateway_processing_requests_report(paths, startup=True, agent=agent)

    assert report.summary["failed"] == 1
    assert report.load_errors[0]["context"] == "gateway.recovery.processing.read"
    response = read_json_file(gateway_response_path(paths, "gw-bad-processing"))
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.recovery.processing.read"
    assert (paths.failed / "gw-bad-processing.json").exists()


def test_copy_final_lease_fields_reports_bad_final_request_json(tmp_path: Path) -> None:
    request_path = tmp_path / "gw-final-bad.json"
    request_path.write_text("{bad json", encoding="utf-8")
    response = {"lease_owner": ""}

    _copy_final_lease_fields(response, request_path)

    assert response["final_request_load_error"]["context"] == "gateway.request_execution.final_request.read"


def test_finish_claimed_request_reports_bad_final_request_archive_json(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-final-archive-bad"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text("{bad json", encoding="utf-8")
    response = {"id": request_id, "ok": True, "status": "done"}

    _finish_claimed_gateway_request(paths, processing_path, request_id, response)

    archived_response = read_json_file(gateway_response_path(paths, request_id))
    assert archived_response["final_request_load_error"]["context"] == "gateway.worker.final_request.read"


def test_finish_claimed_request_archives_chunk_stream_with_request(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-chunk-archive"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        '{"id": "gw-chunk-archive", "kind": "ask", "prompt": "hello", "status": "processing"}',
        encoding="utf-8",
    )
    chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
    chunk_path.write_text('{"t": 1, "text": "hello"}\n', encoding="utf-8")

    _finish_claimed_gateway_request(paths, processing_path, request_id, {"id": request_id, "ok": True, "status": "done"})

    archived_chunk_path = paths.done / f"{request_id}.chunks.jsonl"
    archived_response = read_json_file(gateway_response_path(paths, request_id))
    assert archived_chunk_path.exists()
    assert archived_response["chunk_stream_path"] == str(archived_chunk_path)
    assert not chunk_path.exists()


def test_finish_claimed_request_archives_authoritative_interrupted_status(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-interrupted-archive"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        '{"id": "gw-interrupted-archive", "kind": "ask", "status": "processing"}',
        encoding="utf-8",
    )

    _finish_claimed_gateway_request(
        paths,
        processing_path,
        request_id,
        {"id": request_id, "ok": True, "status": "interrupted", "error_code": "INTERRUPTED"},
    )

    archived_request = read_json_file(paths.done / f"{request_id}.json")
    assert archived_request["status"] == "interrupted"
