from __future__ import annotations

"""Queue iteration and state management for gateway processing.

This module is derived from runtime.py split. It contains queue scanning,
file state transitions, and lease management that were previously in that file.
"""

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .daemon_control import get_running_pid
from .io import (
    append_gateway_history,
    gateway_request_counts,
    gateway_response_path,
    read_json_file,
)

# Re-export heartbeat liveness check for backward compatibility
from .lease_service import is_heartbeat_alive_for_request
from .paths import GatewayPaths, gateway_paths
from .recovery import _archive_gateway_request, _gateway_request_attempts

if TYPE_CHECKING:
    from ...core import SimpleAgent

_CLAIM_LOCK = threading.Lock()


def gateway_running(paths: GatewayPaths) -> tuple[int, bool]:
    pid = get_running_pid(paths.pid)
    return pid, bool(pid)


def wait_for_gateway_running(paths: GatewayPaths, timeout: float = 10.0) -> tuple[int, bool]:
    deadline = time.time() + max(0.0, timeout)
    last_pid = 0
    while True:
        pid, alive = gateway_running(paths)
        if pid:
            last_pid = pid
        if alive:
            return pid, True
        if time.time() >= deadline:
            return pid or last_pid, False
        time.sleep(0.2)


def render_gateway_status(agent: SimpleAgent, paths: GatewayPaths) -> list[str]:
    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    lines = [
        f"gateway status={status} pid={pid if pid else '-'} alive={alive}",
        "gateway requests=" + json.dumps(gateway_request_counts(paths), ensure_ascii=False, sort_keys=True),
    ]
    if heartbeat_at:
        lines.append(f"gateway heartbeat_age_seconds={age:.1f}")
    return lines


def rebuild_gateway_index(agent: SimpleAgent) -> int:
    paths = gateway_paths(agent)
    return (
        _rebuild_gateway_history_index(agent, paths)
        + _rebuild_gateway_request_file_index(agent, paths)
        + _rebuild_gateway_response_index(agent, paths)
    )


def _rebuild_gateway_history_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    from .logging import _index_gateway_payload

    count = 0
    if not paths.history.exists():
        return count
    for line in paths.history.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = _payload_from_history_line(line)
        if not payload:
            continue
        response_path = gateway_response_path(paths, str(payload.get("id") or ""))
        if _index_gateway_payload(agent, payload, response_path=response_path):
            count += 1
    return count


def _payload_from_history_line(line: str) -> dict:
    if not line.strip():
        return {}
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _rebuild_gateway_request_file_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    count = 0
    for request_path in _iter_gateway_request_files(paths):
        if _index_gateway_request_file(agent, paths, request_path):
            count += 1
    return count


def _iter_gateway_request_files(paths: GatewayPaths):
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        yield from sorted(folder.glob("*.json"))


def _index_gateway_request_file(agent: SimpleAgent, paths: GatewayPaths, request_path: Path) -> bool:
    from .logging import _index_gateway_payload

    payload = read_json_file(request_path)
    if not payload:
        return False
    request_id = str(payload.get("id") or request_path.stem)
    response_path = gateway_response_path(paths, request_id)
    response_payload = read_json_file(response_path)
    merged = {**payload, **response_payload} if response_payload else payload
    return _index_gateway_payload(agent, merged, request_path=request_path, response_path=response_path)


def _rebuild_gateway_response_index(agent: SimpleAgent, paths: GatewayPaths) -> int:
    from .logging import _index_gateway_payload

    count = 0
    for response_path in sorted(paths.responses.glob("*.json")):
        payload = read_json_file(response_path)
        if not payload:
            continue
        if _index_gateway_payload(agent, payload, response_path=response_path):
            count += 1
    return count


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


def archive_request(processing_path: Path, target_folder: Path, request_id: str) -> None:
    from .logging import _report_gateway_side_effect_error

    try:
        _archive_gateway_request(processing_path, target_folder)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_request", request_id, exc)
