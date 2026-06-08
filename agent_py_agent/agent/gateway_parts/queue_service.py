"""Queue iteration and state management for gateway processing.

The service owns queue scanning, file state transitions, and lease management.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from .io import (
    GatewayJsonReadReport,
    append_gateway_history,
    gateway_response_path,
    read_json_file_report,
    write_json_file,
)
from .lease_service import is_heartbeat_alive_for_request
from .logging import GatewayIndexPayloadOptions, _index_gateway_payload
from .paths import GatewayPaths, gateway_paths
from .recovery import _archive_gateway_request, _gateway_request_attempts
from .status_rendering import (
    GatewayRunningReport,
    gateway_running,
    gateway_running_report,
    render_gateway_status,
    wait_for_gateway_running,
)

_CLAIM_LOCK = threading.Lock()


@dataclass(frozen=True)
class GatewayIndexRebuildReport:
    indexed_count: int
    load_errors: list[dict[str, Any]]


def rebuild_gateway_index(agent: SimpleAgent) -> int:
    paths = gateway_paths(agent)
    return rebuild_gateway_index_report(agent, paths).indexed_count


def rebuild_gateway_index_report(agent: SimpleAgent, paths: GatewayPaths | None = None) -> GatewayIndexRebuildReport:
    resolved_paths = paths or gateway_paths(agent)
    history = _rebuild_gateway_history_index_report(agent, resolved_paths)
    requests = _rebuild_gateway_request_file_index_report(agent, resolved_paths)
    responses = _rebuild_gateway_response_index_report(agent, resolved_paths)
    return GatewayIndexRebuildReport(
        indexed_count=history.indexed_count + requests.indexed_count + responses.indexed_count,
        load_errors=[*history.load_errors, *requests.load_errors, *responses.load_errors],
    )


def _rebuild_gateway_history_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_history_index_report(agent, paths).indexed_count


def _rebuild_gateway_history_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    if not paths.history.exists():
        return GatewayIndexRebuildReport(count, load_errors)
    try:
        lines = paths.history.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return GatewayIndexRebuildReport(
            count,
            [_gateway_index_load_error(paths.history, exc, "gateway.index.history.read")],
        )
    for line_number, line in enumerate(lines, start=1):
        payload_report = _payload_from_history_line_report(line, paths.history, line_number)
        if payload_report.load_error:
            load_errors.append(payload_report.load_error)
        payload = payload_report.payload
        if not payload:
            continue
        response_path = gateway_response_path(paths, str(payload.get("id") or ""))
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _payload_from_history_line(line: str) -> dict:
    return _payload_from_history_line_report(line).payload


def _payload_from_history_line_report(
    line: str,
    path: Path | None = None,
    line_number: int = 0,
) -> GatewayJsonReadReport:
    if not line.strip():
        return GatewayJsonReadReport({})
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return GatewayJsonReadReport(
            {},
            _gateway_index_load_error(path, exc, "gateway.index.history.read", line_number=line_number),
        )
    if isinstance(payload, dict):
        return GatewayJsonReadReport(payload)
    return GatewayJsonReadReport(
        {},
        _gateway_index_load_error(
            path,
            ValueError(f"gateway history row is {type(payload).__name__}, expected object"),
            "gateway.index.history.read",
            line_number=line_number,
        ),
    )


def _rebuild_gateway_request_file_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_request_file_index_report(agent, paths).indexed_count


def _rebuild_gateway_request_file_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    for request_path in _iter_gateway_request_files(paths):
        indexed, errors = _index_gateway_request_file_report(agent, paths, request_path)
        load_errors.extend(errors)
        if indexed:
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _iter_gateway_request_files(paths: GatewayPaths):
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        yield from sorted(folder.glob("*.json"))


def _index_gateway_request_file(agent: SimpleAgent, paths: GatewayPaths, request_path: Path) -> bool:
    indexed, _ = _index_gateway_request_file_report(agent, paths, request_path)
    return indexed


def _index_gateway_request_file_report(
    agent: SimpleAgent,
    paths: GatewayPaths,
    request_path: Path,
) -> tuple[bool, list[dict[str, Any]]]:
    load_errors: list[dict[str, Any]] = []
    payload_report = read_json_file_report(request_path, context="gateway.index.request.read")
    if payload_report.load_error:
        load_errors.append(payload_report.load_error)
    payload = payload_report.payload
    if not payload:
        return False, load_errors
    request_id = str(payload.get("id") or request_path.stem)
    response_path = gateway_response_path(paths, request_id)
    response_report = read_json_file_report(response_path, context="gateway.index.response_for_request.read")
    if response_report.load_error:
        load_errors.append(response_report.load_error)
    response_payload = response_report.payload
    merged = {**payload, **response_payload} if response_payload else payload
    indexed = _index_gateway_payload(
        agent,
        merged,
        GatewayIndexPayloadOptions(request_path=request_path, response_path=response_path),
    )
    return indexed, load_errors


def _rebuild_gateway_response_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    return _rebuild_gateway_response_index_report(agent, paths).indexed_count


def _rebuild_gateway_response_index_report(agent: SimpleAgent, paths: GatewayPaths) -> GatewayIndexRebuildReport:
    count = 0
    load_errors: list[dict[str, Any]] = []
    for response_path in sorted(paths.responses.glob("*.json")):
        payload_report = read_json_file_report(response_path, context="gateway.index.response.read")
        if payload_report.load_error:
            load_errors.append(payload_report.load_error)
        payload = payload_report.payload
        if not payload:
            continue
        if _index_gateway_payload(agent, payload, GatewayIndexPayloadOptions(response_path=response_path)):
            count += 1
    return GatewayIndexRebuildReport(count, load_errors)


def _gateway_index_load_error(
    path: Path | None,
    exc: BaseException,
    context: str,
    *,
    line_number: int = 0,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    if path is not None:
        report["path"] = str(path)
    if line_number:
        report["line_number"] = line_number
    return report


def ensure_gateway_folders(paths: GatewayPaths) -> None:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)


def claim_request(paths: GatewayPaths, request_path: Path) -> Path | None:
    from .logging import _report_gateway_side_effect_error

    processing_path = paths.processing / request_path.name
    with _CLAIM_LOCK:
        if not request_path.exists() or processing_path.exists():
            return None
        try:
            request_path.replace(processing_path)
            return processing_path
        except OSError as exc:
            _report_gateway_side_effect_error("claim_gateway_request", request_path.stem, exc)
            return None


def archive_request(processing_path: Path, target_folder: Path, request_id: str) -> bool:
    from .logging import _report_gateway_side_effect_error

    try:
        _archive_gateway_request(processing_path, target_folder)
        return True
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
        return False


def materialize_missing_archive(target_folder: Path, request_id: str, response: dict) -> Path:
    target_folder.mkdir(parents=True, exist_ok=True)
    target = target_folder / f"{request_id}.json"
    if target.exists():
        return target
    payload = {
        "id": request_id,
        "status": response.get("status", "done" if response.get("ok") else "failed"),
        "ok": bool(response.get("ok")),
        "attempts": response.get("attempts", 0),
        "lease_owner": response.get("lease_owner", ""),
        "lease_started_at": response.get("lease_started_at", 0),
        "lease_heartbeat_at": response.get("lease_heartbeat_at", 0),
        "completed_at": response.get("ended_at", time.time()),
        "archive_note": "request file was already moved or removed before final archive",
    }
    write_json_file(target, payload)
    return target
