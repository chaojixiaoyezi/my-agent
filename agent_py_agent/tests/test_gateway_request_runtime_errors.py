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
