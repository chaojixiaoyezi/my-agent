
from __future__ import annotations

"""Request execution and handling for gateway."""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import audit_request_queued
from .io import (
    append_gateway_history,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    read_json_file_report,
    write_json_file,
)
from .paths import GatewayPaths, gateway_chunk_path
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


_DEFAULT_GATEWAY_CLI_SESSION_ID = "default"


@dataclass(frozen=True)
class _ClaimedGatewayRequestContext:

    agent: SimpleAgent
    processing_path: Path
    request_payload: dict
    request_id: str
    worker_id: str


@dataclass(frozen=True)
class _PendingGatewayRequest:
    path: Path
    payload: dict


class GatewayInboxScanGate:
    """inbox 空闲扫描门：目录 mtime 没变且上轮扫描为空时跳过 glob。

    粗粒度文件系统（mtime 秒级）保护：mtime 距今不足 2 秒时不跳过，
    避免同一秒内新写入的请求被漏掉。上轮存在 deferred（not_before_at）
    请求时也不跳过，保证延迟请求到点被处理。"""

    _COARSE_MTIME_GUARD_SECONDS = 2.0

    def __init__(self) -> None:
        self._last_mtime_ns: int | None = None
        self._scan_required = True

    def should_scan(self, inbox: Path) -> bool:
        if self._scan_required:
            return True
        mtime_ns = self._inbox_mtime_ns(inbox)
        if mtime_ns is None or mtime_ns != self._last_mtime_ns:
            return True
        return (time.time() - mtime_ns / 1e9) < self._COARSE_MTIME_GUARD_SECONDS

    def record_scan(self, inbox: Path, *, processed: int, deferred_present: bool) -> None:
        self._last_mtime_ns = self._inbox_mtime_ns(inbox)
        self._scan_required = bool(processed or deferred_present or self._last_mtime_ns is None)

    @staticmethod
    def _inbox_mtime_ns(inbox: Path) -> int | None:
        try:
            return inbox.stat().st_mtime_ns
        except OSError:
            return None


def submit_gateway_ask(
    paths: GatewayPaths,
    *,
    params: GatewayAskParams,
) -> tuple[str, Path, Path]:
    from .io import write_gateway_request

    # Request IDs join queue files, responses, chunk streams, and audits across clients.
    request_id = new_gateway_request_id()

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
    payload["conversation"] = _gateway_conversation_payload(params)
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


def _gateway_conversation_payload(params: GatewayAskParams) -> dict:
    session_id = str(params.chat_session_id or _DEFAULT_GATEWAY_CLI_SESSION_ID)
    return {
        "channel": "chat" if params.chat_session_id else "gateway-cli",
        "channel_conversation_id": session_id,
        "channel_user_id": str(params.channel_user_id or "local-cli"),
        "canonical_user_id": str(params.canonical_user_id or "local-agent"),
    }


def wait_for_gateway_response(paths: GatewayPaths, request_id: str, timeout: float) -> dict:
    path = gateway_response_path(paths, request_id)
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        payload = read_gateway_response_file(path, request_id=request_id, context="gateway.worker.response.read")
        if payload:
            return payload
        time.sleep(0.2)
    return {}


def _process_gateway_requests(
    agent: SimpleAgent,
    paths: GatewayPaths,
    *,
    worker_id: str = "gw-worker",
    scan_gate: GatewayInboxScanGate | None = None,
) -> int:
    ensure_gateway_folders(paths)
    if scan_gate is not None and not scan_gate.should_scan(paths.inbox):
        return 0
    processed = 0
    deferred_present = False
    for request in _iter_pending_requests(paths):
        if _request_deferred_until_later(request):
            deferred_present = True
            continue
        if _process_gateway_request_path(agent, paths, request.path, worker_id):
            processed += 1
    if scan_gate is not None:
        scan_gate.record_scan(paths.inbox, processed=processed, deferred_present=deferred_present)
    return processed


def _iter_pending_request_paths(paths: GatewayPaths) -> list[Path]:
    return [entry.path for entry in _iter_pending_requests(paths)]


def _iter_pending_requests(paths: GatewayPaths) -> list[_PendingGatewayRequest]:
    entries = [
        _PendingGatewayRequest(
            path=request_path,
            payload=read_json_file_report(
                request_path,
                context="gateway.worker.pending_scan.read",
            ).payload,
        )
        for request_path in paths.inbox.glob("*.json")
    ]
    return sorted(entries, key=_pending_request_entry_sort_key)


def _pending_request_sort_key(request_path: Path) -> tuple[int, float, str]:
    payload_report = read_json_file_report(request_path, context="gateway.worker.pending_priority.read")
    return _pending_request_entry_sort_key(_PendingGatewayRequest(request_path, payload_report.payload or {}))


def _pending_request_entry_sort_key(request: _PendingGatewayRequest) -> tuple[int, float, str]:
    payload = request.payload or {}
    priority = str(payload.get("priority") or "").strip().lower()
    is_recovery = priority == "recovery" or bool(payload.get("requeued_at"))
    created_at = _request_created_at(payload, request.path)
    return (1 if is_recovery else 0, created_at, request.path.name)


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


def _request_deferred_until_later(request: _PendingGatewayRequest) -> bool:
    payload = request.payload
    if not payload:
        return False
    try:
        not_before = float(payload.get("not_before_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(not_before and time.time() < not_before)


_OWNER_POOL_LOCK = threading.Lock()


def _resolve_request_agent(agent, request_payload: dict):
    """按请求 owner 取作用域 agent(多用户飞书 per-用户隔离)。

    config 开关默认关、或解析不出 owner(匿名/无 channel)、或池出错 → 回退基础 agent(绝不让请求挂、
    不破现有单机/单 owner 行为)。开启后:飞书用户 A/B 各跑在自己 owner 作用域的 agent 上,home/记忆/
    数据/成本/审计天然隔离。
    """
    if not bool(getattr(getattr(agent, "config", None), "gateway_per_user_owner_scoping", False)):
        return agent
    owner = _owner_from_request(agent, request_payload)
    if owner is None:
        return agent
    try:
        return _owner_pool(agent).get(owner)
    except Exception:
        return agent


def _owner_from_request(agent, request_payload: dict):
    """从请求体取 user_id(顶层/metadata)+ channel(metadata)构造 per-用户 owner;匿名/缺字段返回 None。

    用 OwnerIdentity.provider_user(channel, user_id) 确定性构造(每个飞书用户=自己的 owner 作用域,
    无需预注册);区别于 resolve_owner_from_provider_identity(那是查已注册绑定的,未注册返 None)。
    身份绑定(如管理员绑定到 main)是后续细化,这里先做"每用户独立作用域"的基本隔离。
    """
    meta = request_payload.get("metadata") if isinstance(request_payload.get("metadata"), dict) else {}
    user_id = str(request_payload.get("user_id") or meta.get("user_id") or "").strip()
    channel = str(meta.get("channel") or "").strip()
    if not user_id or user_id == "anonymous" or not channel:
        return None
    from ..user_space.owner_resolver import OwnerIdentity

    return OwnerIdentity.provider_user(channel, user_id)


def _config_without_runtime_paths(agent):
    """克隆基础 config 但把已解析的 owner 运行时路径字段(local_store_path/memory_path 等)清空。

    基础 agent 的 __init__ 把这些字段写成了基础 owner(main)的路径,且 resolve_runtime_paths 把"已有值"
    当 override 不再重解析。若池直接克隆这份已固化的 config,作用域 agent 会沿用 main 的 local_store/记忆
    → 不隔离。清空它们,作用域 agent 才会按各自 owner 的 home 重新解析(真隔离)。my_agent_home 是输入
    不在解析字段里,不动。
    """
    import dataclasses

    fields = getattr(getattr(agent, "runtime_path_resolution", None), "paths", {}) or {}
    resets = {name: "" for name in fields if hasattr(agent.config, name)}
    return dataclasses.replace(agent.config, **resets)


def _owner_pool(agent):
    """懒建并缓存挂在基础 agent 上的 owner 池(线程安全双检;多 worker 首次并发只建一个)。"""
    pool = getattr(agent, "_owner_pool", None)
    if pool is not None:
        return pool
    with _OWNER_POOL_LOCK:
        pool = getattr(agent, "_owner_pool", None)
        if pool is None:
            from ..owner_scoped_pool import OwnerScopedAgentPool

            pool = OwnerScopedAgentPool(
                _config_without_runtime_paths(agent), agent.root, workspace_roots=getattr(agent, "workspace_roots", None)
            )
            agent._owner_pool = pool
        return pool


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
        _ClaimedGatewayRequestContext(
            _resolve_request_agent(agent, request_payload),  # 多用户飞书:在请求 owner 作用域的 agent 上跑
            processing_path, request_payload, request_id, worker_id,
        )
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
    target_folder = paths.done if response.get("ok") else paths.failed
    _attach_archived_chunk_stream(paths, request_id, target_folder, response)
    response_path = gateway_response_path(paths, str(response.get("id", processing_path.stem)))
    final_request_load_error = _write_final_request_archive_payload(processing_path, response)
    if final_request_load_error is not None:
        response["final_request_load_error"] = final_request_load_error
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    archived = archive_request(processing_path, target_folder, request_id)
    if not archived and not processing_path.exists():
        materialize_missing_archive(target_folder, request_id, response)


def _attach_archived_chunk_stream(paths: GatewayPaths, request_id: str, target_folder: Path, response: dict) -> None:
    archived_path, archive_error = _archive_gateway_chunk_stream(paths, request_id, target_folder)
    if archived_path is not None:
        response["chunk_stream_path"] = str(archived_path)
    if archive_error is not None:
        response["chunk_stream_archive_error"] = archive_error


def _archive_gateway_chunk_stream(paths: GatewayPaths, request_id: str, target_folder: Path) -> tuple[Path | None, dict | None]:
    chunk_path = gateway_chunk_path(paths, request_id)
    if not chunk_path.exists():
        return None, None
    target_folder.mkdir(parents=True, exist_ok=True)
    target = target_folder / chunk_path.name
    try:
        chunk_path.replace(target)
    except OSError as exc:
        from ..runtime_errors import runtime_error_report

        report = runtime_error_report(exc, context="gateway.worker.chunk_stream.archive")
        report["path"] = str(chunk_path)
        report["target_path"] = str(target)
        return None, report
    return target, None


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
