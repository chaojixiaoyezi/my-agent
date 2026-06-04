
from __future__ import annotations

"""Request execution and handling for gateway."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import audit_request_queued
from .io import (
    append_gateway_history,
    gateway_response_path,
    read_json_file,
    read_json_file_report,
    write_json_file,
)
from .paths import GatewayPaths
from .queue_service import (
    archive_request,
    claim_request,
    ensure_gateway_folders,
    materialize_missing_archive,
)
from .recovery import _gateway_request_attempts
from .request_errors import (
    gateway_request_load_error_response,
    gateway_request_processing_state_error_response,
)
from .request_execution import _handle_gateway_request
from .response_renderer import read_gateway_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent


@dataclass
class GatewayAskParams:
    prompt: str
    inject: list[str] | None = None
    prompt_files: list[str] | None = None
    save: bool = True
    include_prompt: bool = False
    resume_context: bool | None = None
    chat_session_id: str = ""
    channel_user_id: str = "local-cli"
    canonical_user_id: str = "local-agent"
    agent: SimpleAgent | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _ClaimedGatewayRequestContext:

    agent: SimpleAgent
    processing_path: Path
    request_payload: dict
    request_id: str
    worker_id: str


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    from .io import write_gateway_request

    request_id_obj = str(time.time() * 1000)[:13]
    request_id = f"gw-{request_id_obj}"

    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": params.prompt,
        "inject": params.inject or [],
        "prompt_files": params.prompt_files or [],
        "save": params.save,
        "include_prompt": params.include_prompt,
        "created_at": time.time(),
        "client_pid": 0,
        "status": "pending",
        "priority": "interactive",
        "source": "cli_chat" if params.chat_session_id else "cli_gateway",
        "attempts": 0,
    }
    if params.resume_context is not None:
        payload["resume_context"] = bool(params.resume_context)
    if params.chat_session_id:
        payload["conversation"] = {
            "channel": "chat",
            "channel_conversation_id": str(params.chat_session_id),
            "channel_user_id": str(params.channel_user_id or "local-cli"),
            "canonical_user_id": str(params.canonical_user_id or "local-agent"),
        }
    request_path = write_gateway_request(paths, payload)
    response_path = gateway_response_path(paths, request_id)
    if params.agent is not None:
        audit_request_queued(
            params.agent,
            {**payload, "status": "queued", "ok": False},
            request_path,
            response_path,
        )
    return request_id, request_path, response_path


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_gateway_response_file(path, request_id=request_id, context="gateway.worker.response.read")
        if payload:
            return payload
        time.sleep(0.2)
    return {}


def _process_gateway_requests(agent: SimpleAgent, paths: GatewayPaths, *, worker_id: str = "gw-worker") -> int:
    ensure_gateway_folders(paths)
    processed = 0
    for request_path in _iter_pending_request_paths(paths):
        if _request_deferred_until_later(request_path):
            continue
        if _process_gateway_request_path(agent, paths, request_path, worker_id):
            processed += 1
    return processed


def _iter_pending_request_paths(paths: GatewayPaths) -> list[Path]:
    return sorted(paths.inbox.glob("*.json"), key=_pending_request_sort_key)


def _pending_request_sort_key(request_path: Path) -> tuple[int, float, str]:
    payload_report = read_json_file_report(request_path, context="gateway.worker.pending_priority.read")
    payload = payload_report.payload or {}
    priority = str(payload.get("priority") or "").strip().lower()
    is_recovery = priority == "recovery" or bool(payload.get("requeued_at"))
    created_at = _request_created_at(payload, request_path)
    return (1 if is_recovery else 0, created_at, request_path.name)


def _request_created_at(payload: dict, request_path: Path) -> float:
    for key in ("created_at", "submitted_at", "requeued_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0.0


def _request_deferred_until_later(request_path: Path) -> bool:
    payload_report = read_json_file_report(request_path, context="gateway.worker.pending_defer.read")
    payload = payload_report.payload
    if not payload:
        return False
    try:
        not_before = float(payload.get("not_before_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(not_before and time.time() < not_before)


def _process_gateway_request_path(
    agent: SimpleAgent,
    paths: GatewayPaths,
    request_path: Path,
    worker_id: str,
) -> bool:
    processing_path = claim_request(paths, request_path)
    if processing_path is None:
        return False
    request_report = read_json_file_report(processing_path, context="gateway.worker.request.read")
    if request_report.load_error is not None:
        request_id = processing_path.stem
        if gateway_response_path(paths, request_id).exists():
            archive_request(processing_path, paths.done, request_id)
            return True
        response = gateway_request_load_error_response(processing_path, request_report.load_error)
        _finish_claimed_gateway_request(paths, processing_path, request_id, response)
        return True
    request_payload = request_report.payload
    request_id = str(request_payload.get("id") or processing_path.stem)
    request_payload.setdefault("id", request_id)
    if gateway_response_path(paths, request_id).exists():
        archive_request(processing_path, paths.done, request_id)
        return True
    response = _process_claimed_gateway_request(
        _ClaimedGatewayRequestContext(agent, processing_path, request_payload, request_id, worker_id)
    )
    _finish_claimed_gateway_request(paths, processing_path, request_id, response)
    return True


def _process_claimed_gateway_request(context: _ClaimedGatewayRequestContext) -> dict:
    from ..runtime_errors import runtime_error_report
    from .io import write_json_file_atomic
    from .logging import _report_gateway_side_effect_error

    _mark_request_processing(context.request_payload, context.worker_id)
    try:
        write_json_file_atomic(context.processing_path, context.request_payload)
    except OSError as exc:
        _report_gateway_side_effect_error("prepare_gateway_request_lease", context.request_id, exc)
        return gateway_request_processing_state_error_response(
            context.processing_path,
            runtime_error_report(exc, context="gateway.worker.processing_state.write"),
            request_id=context.request_id,
        )
    return _handle_gateway_request(
        context.agent,
        context.processing_path,
        refresh_lease=True,
        worker_id=context.worker_id,
    )


def _mark_request_processing(request_payload: dict, worker_id: str) -> None:
    lease_now = time.time()
    request_payload.update(
        {
            "status": "processing",
            "attempts": _gateway_request_attempts(request_payload) + 1,
            "lease_owner": worker_id,
            "lease_started_at": lease_now,
            "lease_heartbeat_at": lease_now,
            "updated_at": lease_now,
        }
    )


def _finish_claimed_gateway_request(
    paths: GatewayPaths,
    processing_path: Path,
    request_id: str,
    response: dict,
) -> None:
    response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
    final_request_load_error = _write_final_request_archive_payload(processing_path, response)
    if final_request_load_error is not None:
        response["final_request_load_error"] = final_request_load_error
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    target_folder = paths.done if response.get("ok") else paths.failed
    archived = archive_request(processing_path, target_folder, request_id)
    if not archived and not processing_path.exists():
        materialize_missing_archive(target_folder, request_id, response)


def _write_final_request_archive_payload(processing_path: Path, response: dict) -> dict | None:
    report = read_json_file_report(processing_path, context="gateway.worker.final_request.read")
    if report.load_error is not None:
        return report.load_error
    request_payload = report.payload
    if not request_payload:
        return None
    request_payload.update(
        {
            "status": str(request_payload.get("status") or response.get("status") or ""),
            "attempts": response.get("attempts", request_payload.get("attempts", 0)),
            "lease_owner": response.get("lease_owner", request_payload.get("lease_owner", "")),
            "lease_started_at": response.get("lease_started_at", request_payload.get("lease_started_at", 0)),
            "lease_heartbeat_at": response.get("lease_heartbeat_at", request_payload.get("lease_heartbeat_at", 0)),
            "completed_at": response.get("ended_at", time.time()),
        }
    )
    write_json_file(processing_path, request_payload)
    return None
