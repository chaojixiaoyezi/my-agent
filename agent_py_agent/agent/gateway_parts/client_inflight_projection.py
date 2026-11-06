"""从原请求事实只读发现同 owner/thread 的活动回合，不创建 Agent 或写入状态。"""
from __future__ import annotations

import hashlib
import math
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .control_service import _live_window_requests, _matching_requests, _scope_request_payload
from .paths import gateway_chunk_path
from .request_worker import _resolve_request_owner_from_config


@dataclass
class _Cursor:
    """可丢弃的位置缓存，不保存正文，也不提供请求状态或权限。"""

    identity: tuple[int, int]
    version: tuple[int, int, int]
    offset: int = 0
    lines: int = 0
    boundary: bytes = b""


_CURSORS: OrderedDict[str, _Cursor] = OrderedDict()
_CURSOR_LOCK = threading.Lock()


def client_inflight_projection(
    server, scope, thread_id: str, *,
    max_request_files: int = 512,
    max_request_bytes: int = 1_048_576,
    max_chunk_scan_bytes: int = 1_048_576,
    max_cached_streams: int = 64,
) -> dict[str, object]:
    """history 原鉴权与读取成功后调用；预算不足保守回放，不推进未知游标。"""
    result = _idle_projection()
    if not thread_id or not scope.user_id or not scope.channel or not scope.conversation_id:
        return result
    try:
        paths = server.paths
        control, runtime = getattr(server, "control_plane", None), getattr(server, "drain_runtime", None)
        _check_source(server, control, runtime, paths)
        agent = getattr(server, "agent", None)
        source = control if control is not None else agent
        config = source.config
        home_root = (control.home_root if control is not None
                     else getattr(getattr(agent, "home_paths", None), "root", None))
        expected_owner = _resolve_request_owner_from_config(config, _scope_request_payload(scope), home_root)
        # 原控制选择器扫描单层请求文件；先限制该目录的资源，不遍历历史或归档。
        if not _within_request_budget(paths, max_request_files, max_request_bytes):
            return result

        def allowed(record):
            payload = record.payload
            if _resolve_request_owner_from_config(config, payload, home_root) != expected_owner:
                return False
            claim = payload.get("conversation_claim")
            if claim is not None and not isinstance(claim, dict):
                return False
            # 排队请求尚无执行 claim，沿原精确 scope；已有绑定必须全部吻合。
            bindings = [payload.get("conversation_thread_id"), (claim or {}).get("thread_id")]
            return all(not value or str(value) == thread_id for value in bindings)

        processing = [record for record in _matching_requests(paths.processing, scope)
                      if allowed(record) and str(record.payload.get("status") or "processing")
                      not in {"done", "failed", "cancelled", "stopped", "interrupted"}]
        # 原 helper 是开放回合权威；它排除 closing/stopping，但发现接口必须继续展示停止中。
        live_paths = {record.path for record in _live_window_requests(paths, scope)}
        active = [record for record in processing if record.path in live_paths
                  or record.payload.get("control_status") == "stopping"
                  or record.payload.get("cancel_requested") is True
                  or record.payload.get("turn_phase") == "closing"]
        pending = [record for record in _matching_requests(paths.inbox, scope) if allowed(record)
                   and str(record.payload.get("status") or "pending") == "pending"]
        pending.sort(key=_queue_order)
        queued = [_request_id(record) for record in pending]
        result["queued_request_ids"] = list(dict.fromkeys(value for value in queued if value))
        # 多个前台回合是恢复冲突，不能凭最新时间戳臆造一个活动权威。
        if len(active) <= 1:
            selected = active[0] if active else (pending[0] if pending else None)
            if selected is not None:
                request_id = _request_id(selected)
                if request_id:
                    payload = selected.payload
                    stopping = (payload.get("control_status") == "stopping"
                                or payload.get("cancel_requested") is True)
                    cursor = (_stream_cursor(gateway_chunk_path(paths, request_id),
                                             max_scan_bytes=max_chunk_scan_bytes,
                                             max_cached_streams=max_cached_streams) if active else 0)
                    result.update(
                        active_request_id=request_id,
                        in_flight_status="stopping" if stopping else ("processing" if active else "pending"),
                        turn_phase=str(payload.get("turn_phase") or ("open" if active else "")),
                        stream_cursor=cursor,
                        progress_path=f"/progress/{quote(request_id, safe='')}?since={cursor}",
                    )
        _check_source(server, control, runtime, paths)
    except Exception:  # noqa: BLE001 附加信息失败不泄露路径、异常或破坏原历史。
        return _idle_projection()
    return result


def _idle_projection() -> dict[str, object]:
    return {"active_request_id": "", "in_flight_status": "idle", "turn_phase": "",
            "stream_cursor": 0, "progress_path": "", "queued_request_ids": []}


def _check_source(server, control, runtime, paths) -> None:
    """C 原来源在投影首末均须有效；部分/复制/关闭的 runtime 不回退单进程。"""
    if (server.paths is not paths or getattr(server, "control_plane", None) is not control
            or getattr(server, "drain_runtime", None) is not runtime):
        raise ValueError("http_original_control_required")
    if control is not None or runtime is not None:
        from .control_plane import GatewayControlPlane
        from .multiprocess_http_transport import require_http_transport

        if type(control) is not GatewayControlPlane:
            raise ValueError("http_original_control_required")
        require_http_transport(runtime, control=control, paths=paths)


def _within_request_budget(paths, max_files: int, max_bytes: int) -> bool:
    """限定原控制读口的单层扫描规模，过载时不返回截断的排队身份清单。"""
    if max_files <= 0 or max_bytes <= 0:
        return False
    count, size = 0, 0
    for folder in (paths.processing, paths.inbox):
        if not folder.exists():
            continue
        with os.scandir(folder) as entries:
            for entry in entries:
                count += 1
                if count > max_files:
                    return False
                if not entry.name.endswith(".json"):
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    return False
                size += info.st_size
                if size > max_bytes:
                    return False
    return True


def _request_id(record) -> str:
    value = str(record.payload.get("id") or record.path.stem)
    # 损坏记录不得用 ID 逃出原文件或跳转到另一请求。
    if not value or value in {".", ".."} or "/" in value or "\\" in value or value != record.path.stem:
        return ""
    return value


def _queue_order(record) -> tuple[float, str]:
    """展示按 created_at 与稳定 ID 排序；异常时间不抢在合法请求前面。"""
    value = record.payload.get("created_at")
    try:
        timestamp = float(value)
    except (ValueError, TypeError):
        timestamp = math.inf
    if not math.isfinite(timestamp) or timestamp < 0 or isinstance(value, bool):
        timestamp = math.inf
    return timestamp, _request_id(record)


def _boundary(handle, end: int, *, sample_bytes: int = 64) -> bytes:
    size = min(sample_bytes, end)
    handle.seek(0)
    head = handle.read(size)
    handle.seek(end - size)
    return hashlib.sha256(head + handle.read(size)).digest()


def _open_chunk_file(path: Path):
    """拒绝链接与非普通文件；非阻塞打开避免坏记录把只读轮询卡在 FIFO。"""
    if path.is_symlink():
        raise OSError("chunk_symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("chunk_not_regular")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _stream_cursor(path: Path, *, max_scan_bytes: int, max_cached_streams: int) -> int:
    """最多扫描预算字节，只计完整 LF；不足时给已确认前缀，宁可重放不跳过。"""
    if max_scan_bytes <= 0 or max_cached_streams <= 0:
        return 0
    key = str(path)
    try:
        with _CURSOR_LOCK, _open_chunk_file(path) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return 0
            identity = (info.st_dev, info.st_ino)
            version = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            old = _CURSORS.get(key)
            if old is not None and (old.identity != identity or info.st_size < old.offset
                                    or (info.st_size == old.version[0] and version != old.version)
                                    or _boundary(handle, old.offset) != old.boundary):
                old = None
            index = old if old is not None else _Cursor(identity, version)
            handle.seek(index.offset)
            data = handle.read(min(max_scan_bytes, max(0, info.st_size - index.offset)))
            index.offset += len(data)
            index.lines += data.count(b"\n")
            index.version = version
            index.boundary = _boundary(handle, index.offset)
            _CURSORS[key] = index
            _CURSORS.move_to_end(key)
            while len(_CURSORS) > max_cached_streams:
                _CURSORS.popitem(last=False)
            return index.lines
    except OSError:
        with _CURSOR_LOCK:
            _CURSORS.pop(key, None)
        return 0


# LLM: 只读一次有界原请求窗口，复用原 owner/scope/claim 和开放回合规则；首末验证同一来源，不读 chunks 或创建宿主。
# 函数用途: 为一页会话批量计算运行/排队状态，恢复冲突不猜活动请求，失败只公开覆盖范围。
def client_inflight_index(
    server, owner_scope, entries, *,
    max_request_files: int = 512,
    max_request_bytes: int = 1_048_576,
) -> tuple[dict[str, dict[str, object]], str]:
    """一次有界读取 processing/inbox，按每个绑定的原 scope 规则给出 running/queued；从不抛出。"""
    try:
        from .control_service import (
            _GatewayRequestRecord,
            _request_accepts_active_input,
            _request_matches_scope,
        )
        from .io import read_json_file_report

        paths = server.paths
        control, runtime = getattr(server, "control_plane", None), getattr(server, "drain_runtime", None)
        _check_source(server, control, runtime, paths)
        agent = getattr(server, "agent", None)
        source = control if control is not None else agent
        config = source.config
        home_root = (control.home_root if control is not None
                     else getattr(getattr(agent, "home_paths", None), "root", None))
        expected_owner = _resolve_request_owner_from_config(config, _scope_request_payload(owner_scope), home_root)
        if not _within_request_budget(paths, max_request_files, max_request_bytes):
            return {}, "partial"
        folders = []
        for folder in (paths.processing, paths.inbox):
            records = []
            for path in folder.glob("*.json"):
                report = read_json_file_report(path, context="gateway.control.request.read")
                if report.load_error is not None or not report.payload:
                    continue
                payload = dict(report.payload)
                if _resolve_request_owner_from_config(config, payload, home_root) == expected_owner:
                    records.append(_GatewayRequestRecord(path, payload))
            folders.append(records)
        processing, inbox = folders
        result = {}
        for key, (scope, thread_id) in entries.items():
            # LLM: owner 已在窗口入口过滤；绑定校验仍与单线程投影一致，排队请求允许尚未生成 claim。
            # 函数用途: 排除线程或 claim 绑定不符的请求记录。
            def bound(record):
                payload = record.payload
                claim = payload.get("conversation_claim")
                if claim is not None and not isinstance(claim, dict):
                    return False
                bindings = [payload.get("conversation_thread_id"), (claim or {}).get("thread_id")]
                return all(not value or str(value) == thread_id for value in bindings)

            active = [record for record in processing
                      if _request_matches_scope(record.payload, scope) and bound(record)
                      and str(record.payload.get("status") or "processing")
                      not in {"done", "failed", "cancelled", "stopped", "interrupted"}
                      and (_request_accepts_active_input(record)
                           or record.payload.get("control_status") == "stopping"
                           or record.payload.get("cancel_requested") is True
                           or record.payload.get("turn_phase") == "closing")]
            pending = [record for record in inbox
                       if _request_matches_scope(record.payload, scope) and bound(record)
                       and str(record.payload.get("status") or "pending") == "pending"]
            pending.sort(key=_queue_order)
            if len(active) > 1:
                result[key] = {"status": "running", "active_request_id": "", "active_run_id": ""}
            elif active:
                record = active[0]
                authority = record.payload.get("runtime_authority")
                result[key] = {"status": "running", "active_request_id": _request_id(record),
                               "active_run_id": str(authority.get("run_id") or "") if isinstance(authority, dict) else ""}
            elif pending:
                result[key] = {"status": "queued", "active_request_id": _request_id(pending[0]), "active_run_id": ""}
        _check_source(server, control, runtime, paths)
        return result, "complete"
    except Exception:  # noqa: BLE001 附加信息失败不泄露路径、异常或破坏原历史。
        return {}, "unavailable"
