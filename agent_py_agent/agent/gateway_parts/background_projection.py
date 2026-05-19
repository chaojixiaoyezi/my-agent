# LLM: Gateway background request projections must stay refs-first and bounded.
# 模块用途: 从 gateway 请求、响应和 chunk 文件生成前台状态查询可用的轻量视图。

"""Projection helpers for background gateway requests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .io import gateway_response_path, read_json_file
from .paths import GatewayPaths, gateway_chunk_path

_QUEUE_FOLDERS = ("processing", "pending", "failed", "done")
_PREVIEW_CHARS = 240


# LLM: background request status must come from gateway files, not prompt wording.
# 函数用途: 汇总 no-wait 后台主代理请求，供 status/control_plane 前台查询复用。
def gateway_background_request_snapshots(paths: GatewayPaths, *, limit: int = 10, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    snapshots: list[dict] = []
    for folder_name in _QUEUE_FOLDERS:
        folder = getattr(paths, folder_name, None)
        if folder is None:
            continue
        for request_path in sorted(folder.glob("*.json")):
            payload = read_json_file(request_path)
            if not payload or bool(payload.get("client_wait", True)):
                continue
            request_id = str(payload.get("id") or request_path.stem)
            snapshots.append(
                _background_request_snapshot(
                    paths,
                    request_path=request_path,
                    request_id=request_id,
                    queue_status=folder_name,
                    payload=payload,
                    now=now,
                )
            )
    snapshots.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
    return snapshots[: max(0, int(limit))]


# LLM: _background_request_snapshot keeps request/response/chunk refs small enough for foreground status.
# 函数用途: 把单条后台请求文件转换成稳定 JSON 视图，正文只给预览和路径引用。
def _background_request_snapshot(
    paths: GatewayPaths,
    *,
    request_path: Path,
    request_id: str,
    queue_status: str,
    payload: dict,
    now: float,
) -> dict:
    response_path = gateway_response_path(paths, request_id)
    response = read_json_file(response_path)
    chunk_path = gateway_chunk_path(paths, request_id)
    chunk = _last_chunk_preview(chunk_path)
    lease_at = float(payload.get("lease_heartbeat_at") or 0)
    return {
        "request_id": request_id,
        "status": str(response.get("status") or payload.get("status") or queue_status),
        "queue_status": queue_status,
        "created_at": payload.get("created_at", 0),
        "updated_at": payload.get("updated_at", payload.get("created_at", 0)),
        "attempts": int(payload.get("attempts") or 0),
        "lease_owner": str(payload.get("lease_owner") or ""),
        "lease_heartbeat_age_seconds": round(now - lease_at, 1) if lease_at else 0,
        "request_path": str(request_path),
        "response_path": str(response_path),
        "chunk_path": str(chunk_path) if chunk_path.exists() else "",
        "response_ok": response.get("ok") if response else None,
        "response_status": response.get("status", "") if response else "",
        "backend": response.get("backend", "") if response else "",
        "tool_rounds": int(response.get("tool_rounds") or 0) if response else 0,
        "duration_seconds": response.get("duration_seconds", 0) if response else 0,
        "last_chunk_at": chunk["t"],
        "last_chunk_preview": chunk["text"],
    }


# LLM: _last_chunk_preview reads the newest chunk line without inlining the full stream.
# 函数用途: 从 chunks.jsonl 读取最后一段文本预览；解析失败时返回空结构。
def _last_chunk_preview(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"t": 0, "text": ""}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"t": 0, "text": ""}
    for line in reversed(lines[-20:]):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(payload.get("text") or "").strip()
        return {"t": payload.get("t", 0), "text": _preview(text)}
    return {"t": 0, "text": ""}


# LLM: _preview keeps status payloads bounded even when chunks contain large streamed output.
# 函数用途: 截断前台状态预览文本，避免把完整模型流或产物内容塞进 control_plane/status。
def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS].rstrip() + "..."
