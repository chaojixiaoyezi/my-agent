from __future__ import annotations

"""LLM: lightweight recovery snapshot builder for run/gateway/subagent completion points.

给人看的解释：
这个文件专门负责写“恢复锚点”。
它不保存大段工具输出，只保存用户意图、助手动作、工具摘要、任务/请求 ID、恢复路径和 token 估算。
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import CompressionSnapshot, utc_now_iso
from .storage import append_snapshot
from .tokens import estimate_tokens


SNAPSHOT_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


@dataclass(frozen=True)
class RecoverySnapshotResult:
    """LLM: result returned after a best-effort recovery snapshot write.

    给人看的解释：
    写快照成功时这里会有 snapshot_id 和 path。
    如果失败，主流程不崩，error 会说明为什么没写成。
    """

    ok: bool
    snapshot_id: str = ""
    path: str = ""
    token_estimate: int = 0
    error: str = ""


def write_recovery_snapshot(
    root: str | Path,
    *,
    session_id: str,
    user_prompt: str,
    response_text: str,
    backend: str,
    source: str,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    status: str = "ok",
    error_code: str = "",
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    task_refs: Iterable[str] | None = None,
    content_paths: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    archive_level: int = 3,
    created_at: str | None = None,
) -> RecoverySnapshotResult:
    """LLM: write one minimal hook snapshot and return a non-throwing result.

    大白话：每轮任务结束时调用它，写一条很小的 JSONL。
    它会尽量保留恢复需要的字段，但如果磁盘或 JSONL 出问题，只返回 error，不拖死用户当前请求。
    """

    timestamp = created_at or utc_now_iso()
    level = _normalize_archive_level(archive_level)
    normalized_tools = [_tool_snapshot(item, level) for item in tool_calls or []]
    clean_task_refs = _dedupe_texts([run_id, task_id, *(task_refs or [])])
    clean_content_paths = _dedupe_texts(content_paths or [])
    clean_next_actions = _dedupe_texts(next_actions or [])
    token_estimate = estimate_tokens(
        {
            "user_prompt": user_prompt,
            "response_text": response_text,
            "tool_calls": normalized_tools,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
        }
    )
    snapshot_id = _snapshot_id(
        {
            "created_at": timestamp,
            "session_id": session_id,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "source": source,
            "status": status,
            "user_hash": _content_hash(user_prompt),
            "response_hash": _content_hash(response_text),
        }
    )
    snapshot = CompressionSnapshot(
        snapshot_id=snapshot_id,
        session_id=str(session_id),
        compression_id=f"recovery:{source}:{request_id or run_id or task_id or snapshot_id[-8:]}",
        turn_range={
            "kind": "recovery_snapshot",
            "source": source,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
        },
        participants=_participants(normalized_tools),
        user_intents=[_preview(user_prompt, level)] if user_prompt else [],
        assistant_actions=[_preview(response_text, level)] if response_text else [],
        tool_calls=normalized_tools,
        dispatch_events=[
            {
                "source": source,
                "request_id": request_id,
                "run_id": run_id,
                "task_id": task_id,
                "status": status,
                "error_code": error_code,
                "backend": backend,
            }
        ],
        task_refs=clean_task_refs,
        decisions=[],
        open_questions=[],
        next_actions=clean_next_actions,
        token_usage={
            "estimate": token_estimate,
            "archive_level": level,
            "backend": backend,
        },
        archive_level=level,
        content_paths=clean_content_paths,
        created_at=timestamp,
    )
    try:
        path = append_snapshot(root, snapshot)
    except Exception as exc:
        return RecoverySnapshotResult(
            ok=False,
            snapshot_id=snapshot_id,
            token_estimate=token_estimate,
            error=f"{type(exc).__name__}: {exc}",
        )
    return RecoverySnapshotResult(
        ok=True,
        snapshot_id=snapshot_id,
        path=str(path),
        token_estimate=token_estimate,
    )


def _tool_snapshot(tool_call: Mapping[str, Any], archive_level: int) -> dict[str, Any]:
    """LLM: convert a tool call record into small recovery-safe metadata.

    大白话：这里只保留工具名、调用 ID、成功失败、错误码和参数预览。
    工具返回的大内容不进 hook，只保留 hash/路径这类恢复线索。
    """

    parameters = tool_call.get("parameters", {})
    return {
        "tool": str(tool_call.get("tool") or tool_call.get("tool_name") or "unknown"),
        "tool_call_id": str(tool_call.get("id") or tool_call.get("tool_call_id") or ""),
        "ok": tool_call.get("ok"),
        "status": str(tool_call.get("status") or ""),
        "error_code": str(tool_call.get("error_code") or ""),
        "parameters_preview": _preview(_stable_json(parameters), archive_level),
    }


def _participants(tool_calls: list[dict[str, Any]]) -> list[str]:
    participants = ["user", "assistant"]
    if tool_calls:
        participants.append("tool")
    return participants


def _normalize_archive_level(value: int) -> int:
    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    return level if 0 <= level <= 3 else 3


def _preview(content: str, archive_level: int) -> str:
    limit = SNAPSHOT_PREVIEW_LIMITS[_normalize_archive_level(archive_level)]
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _snapshot_id(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"snapshot:{digest[:32]}"


def _content_hash(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
