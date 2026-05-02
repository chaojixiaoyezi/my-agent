"""LLM: low-level event builders, normalizers, and hashing helpers for run-turn archiving.

给人看的解释：
这个文件放所有"构造单条归档事件"和"辅助工具函数"。
包括用户/助手消息事件、工具调用事件、工具元数据构建，
以及归一化、预览裁剪、hash、稳定 JSON 序列化等纯函数。
主入口 turn_archiver.archive_run_turn 只调用 _build_run_turn_events，不直接碰磁盘。
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any, Mapping

from ..models import RawMemoryEvent

_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


def _message_event(
    *,
    sequence: int,
    session_id: str,
    request_id: str,
    run_id: str,
    task_id: str,
    speaker: str,
    target: str,
    action: str,
    content: str,
    backend: str,
    source: str,
    archive_level: int,
    created_at: str,
) -> RawMemoryEvent:
    """LLM: create one user or assistant message archive event.

    新手说明:
    用户消息和助手回复字段形状基本一样，只是 speaker、target 和 action 不同。
    这里统一生成 event_id、短预览和内容 hash，避免两边格式漂移。
    """
    content_hash = _content_hash(content)
    event_id = _event_id(
        {
            "kind": "message",
            "sequence": sequence,
            "session_id": session_id,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "speaker": speaker,
            "target": target,
            "action": action,
            "backend": backend,
            "source": source,
            "content_hash": content_hash,
        }
    )
    return RawMemoryEvent(
        event_id=event_id,
        session_id=session_id,
        request_id=request_id,
        run_id=run_id,
        speaker=speaker,
        target=target,
        action=action,
        created_at=created_at,
        status="ok",
        task_id=task_id,
        content_preview=_preview(content, archive_level),
        content_path="",
        content_hash=content_hash,
        visibility="private",
        source=source,
    )


def _tool_event(
    *,
    sequence: int,
    session_id: str,
    request_id: str,
    run_id: str,
    task_id: str,
    backend: str,
    tool_call: dict[str, Any],
    source: str,
    archive_level: int,
    created_at: str,
) -> RawMemoryEvent:
    """LLM: create one tool-call archive event from normalized tool metadata.

    新手说明:
    工具调用可能来自不同后端，字段名不完全一样。这个函数先提取常见字段，
    再把输出正文变成 hash 和短预览，避免 raw archive 暴涨。
    """
    tool_name = _first_text(tool_call, "tool_name", "tool", "name") or "unknown"
    tool_call_id = _first_text(tool_call, "tool_call_id", "call_id", "id")
    tool_success = _first_bool(tool_call, "tool_success", "success", "ok")
    status = _tool_status(tool_call, tool_success)
    error_code = _first_text(tool_call, "error_code", "code")
    metadata = _tool_metadata(
        tool_call,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_success=tool_success,
        status=status,
        error_code=error_code,
        backend=backend,
    )
    metadata_text = _stable_display_json(metadata)
    content_hash = _content_hash(_canonical_json(tool_call))
    event_id = _event_id(
        {
            "kind": "tool",
            "sequence": sequence,
            "session_id": session_id,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "backend": backend,
            "source": source,
            "content_hash": content_hash,
        }
    )
    return RawMemoryEvent(
        event_id=event_id,
        session_id=session_id,
        request_id=request_id,
        run_id=run_id,
        speaker="tool",
        target="assistant",
        action="tool_call",
        created_at=created_at,
        status=status,
        error_code=error_code,
        is_dispatch=bool(tool_call.get("is_dispatch", False)),
        task_id=task_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_success=tool_success,
        content_preview=_preview(metadata_text, archive_level),
        content_path="",
        content_hash=content_hash,
        visibility="private",
        source=source,
    )


def _tool_metadata(
    tool_call: dict[str, Any],
    *,
    tool_name: str,
    tool_call_id: str,
    tool_success: bool | None,
    status: str,
    error_code: str,
    backend: str,
) -> dict[str, Any]:
    """LLM: build the bounded metadata preview stored for a tool event.

    新手说明:
    工具结果可能很长，甚至包含敏感内容。这里只保留状态、参数和输出摘要；
    完整输出以后应走 content_path 或 evidence 文件，而不是塞进 raw event。
    """
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "success": tool_success,
        "status": status,
        "error_code": error_code,
        "backend": backend,
    }
    for key in ("output", "result", "response", "content"):
        if key in tool_call:
            text = str(tool_call[key])
            metadata[f"{key}_hash"] = _content_hash(text)
            metadata[f"{key}_preview"] = _preview(text, 3)
            break
    for key in ("parameters", "params", "arguments", "args"):
        if key in tool_call:
            metadata[key] = tool_call[key]
            break
    return metadata


def _normalize_tool_call(call: Any) -> dict[str, Any]:
    """LLM: coerce arbitrary tool-call objects into a plain dictionary.

    新手说明:
    测试和后端可能传 dict、dataclass 或普通对象。归档层只想处理字典，
    所以这里把常见形状统一成 dict；未知形状至少保存字符串值。
    """
    if isinstance(call, Mapping):
        return dict(call)
    if is_dataclass(call) and not isinstance(call, type):
        return asdict(call)
    if hasattr(call, "__dict__"):
        return {
            key: value
            for key, value in vars(call).items()
            if not key.startswith("_")
        }
    return {"value": str(call)}


def _normalize_archive_level(value: int) -> int:
    """LLM: keep archive levels inside the supported 0..3 range.

    新手说明:
    归档级别越小，预览越长；越大，预览越短。坏值统一回到 3，减少隐私和体积风险。
    """
    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    if level < 0 or level > 3:
        return 3
    return level


def _preview(content: str, archive_level: int) -> str:
    """LLM: trim archived text according to archive level.

    新手说明:
    raw event 只保存短预览，既能让人排查，又不会把长正文全部塞进索引行。
    """
    limit = _PREVIEW_LIMITS[_normalize_archive_level(archive_level)]
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _event_id(payload: Mapping[str, Any]) -> str:
    """LLM: derive a stable raw event id from canonical payload facts.

    新手说明:
    同一轮、同一内容、同一工具调用会得到同样 ID，方便测试和去重。
    """
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"raw:{digest[:32]}"


def _content_hash(content: str) -> str:
    """LLM: compute a SHA-256 content hash with an explicit prefix.

    新手说明:
    hash 让我们以后能确认正文是否变化，而不用把完整正文都放在索引里。
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_json(payload: Any) -> str:
    """LLM: serialize payloads into deterministic JSON for identity hashes.

    新手说明:
    字典字段顺序不同不应该影响事件 ID。排序后的 JSON 能保证 hash 稳定。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_display_json(payload: Any) -> str:
    """LLM: serialize metadata for human-facing previews while keeping input order.

    新手说明:
    展示预览更希望保留人工构造的字段顺序，所以这里不排序。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"), default=str)


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """LLM: find the first non-empty text value among candidate keys.

    新手说明:
    不同工具后端可能把同一个概念叫 `tool_name`、`tool` 或 `name`。
    这个 helper 按优先级找第一个可用字段。
    """
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _first_bool(payload: Mapping[str, Any], *keys: str) -> bool | None:
    """LLM: find the first boolean-ish status among candidate keys.

    新手说明:
    工具成功状态可能是真布尔，也可能是 `"yes"`、`"failed"` 这类字符串。
    这里统一转成 `True`、`False` 或未知。
    """
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "ok", "success"}:
                return True
            if text in {"false", "0", "no", "error", "failed", "failure"}:
                return False
    return None


def _tool_status(payload: Mapping[str, Any], tool_success: bool | None) -> str:
    """LLM: choose a stable tool status string.

    新手说明:
    如果工具自己给了 status，就尊重它；否则根据 success 推出 ok/error；再不确定就是 unknown。
    """
    status = _first_text(payload, "status")
    if status:
        return status
    if tool_success is True:
        return "ok"
    if tool_success is False:
        return "error"
    return "unknown"
