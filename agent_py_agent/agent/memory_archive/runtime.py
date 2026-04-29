from __future__ import annotations

"""LLM: build and append raw archive events for one SimpleAgent run turn.

给人看的解释：
这里是主循环以后接冷归档时要用的“小装配台”。
它只接收一轮 run 已经产生的用户输入、助手输出和工具元数据，然后统一生成可检索的 raw 事件，避免主循环里到处手写字段。
"""

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import RawMemoryEvent, utc_now_iso
from .storage import append_raw_event
from .tokens import estimate_tokens


_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


@dataclass(frozen=True)
class ArchiveRunTurnResult:
    """LLM: return summary for raw archive writes performed for one run turn.

    给人看的解释：
    调用方拿到这个结果后，可以知道这轮写到了哪些文件、写了几条事件、粗略占多少 token。
    里面也带回 event_id 和 content_hash，方便测试、排障或后续把归档记录和请求日志串起来。
    """

    write_paths: tuple[Path, ...]
    event_count: int
    token_estimate: int
    event_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    events: tuple[RawMemoryEvent, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        return self.write_paths

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize the archive result without losing event details.

        给人看的解释：
        如果 CLI 或 doctor 想把这次归档结果打印成 JSON，可以直接用这个方法。
        路径会转成字符串，事件会转成普通字典。
        """

        return {
            "write_paths": [str(path) for path in self.write_paths],
            "event_count": self.event_count,
            "token_estimate": self.token_estimate,
            "event_ids": list(self.event_ids),
            "content_hashes": list(self.content_hashes),
            "events": [event.to_dict() for event in self.events],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def archive_run_turn(
    root: str | Path,
    *,
    session_id: str,
    user_prompt: str,
    response_text: str,
    backend: str,
    tool_calls: Iterable[Any] | None = None,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    source: str = "run",
    archive_level: int = 3,
    created_at: str | None = None,
) -> ArchiveRunTurnResult:
    """LLM: append user, assistant, and optional tool RawMemoryEvent records for one run turn.

    给人看的解释：
    真实 `SimpleAgent.run()` 后面只要把本轮已经知道的信息丢进来，就能得到统一格式的冷归档。
    这里不会保存 system prompt 或内置 prompt，也不会把长正文写成 blob；目前只保留短预览、hash 和空的正文路径占位。
    """

    normalized_level = _normalize_archive_level(archive_level)
    timestamp = created_at or utc_now_iso()
    normalized_tool_calls = [_normalize_tool_call(call) for call in tool_calls or []]
    events = _build_run_turn_events(
        session_id=str(session_id),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        tool_calls=normalized_tool_calls,
        request_id=str(request_id),
        run_id=str(run_id),
        task_id=str(task_id),
        source=str(source),
        archive_level=normalized_level,
        created_at=timestamp,
    )

    paths: list[Path] = []
    for event in events:
        path = append_raw_event(root, event)
        if path not in paths:
            paths.append(path)

    token_estimate = estimate_tokens(
        {
            "user_prompt": user_prompt,
            "response_text": response_text,
            "backend": backend,
            "tool_calls": normalized_tool_calls,
        }
    )

    return ArchiveRunTurnResult(
        write_paths=tuple(paths),
        event_count=len(events),
        token_estimate=token_estimate,
        event_ids=tuple(event.event_id for event in events),
        content_hashes=tuple(event.content_hash for event in events),
        events=tuple(events),
    )


def _build_run_turn_events(
    *,
    session_id: str,
    user_prompt: str,
    response_text: str,
    backend: str,
    tool_calls: list[dict[str, Any]],
    request_id: str,
    run_id: str,
    task_id: str,
    source: str,
    archive_level: int,
    created_at: str,
) -> list[RawMemoryEvent]:
    """LLM: convert one run turn into ordered RawMemoryEvent objects without writing them.

    给人看的解释：
    这一步只拼事件，不碰磁盘。
    先放用户消息，再放助手回答，再按原顺序放工具元数据，后面测试稳定性也主要看这里。
    """

    events = [
        _message_event(
            sequence=1,
            session_id=session_id,
            request_id=request_id,
            run_id=run_id,
            task_id=task_id,
            speaker="user",
            target="assistant",
            action="message",
            content=user_prompt,
            backend=backend,
            source=source,
            archive_level=archive_level,
            created_at=created_at,
        ),
        _message_event(
            sequence=2,
            session_id=session_id,
            request_id=request_id,
            run_id=run_id,
            task_id=task_id,
            speaker="assistant",
            target="user",
            action="response",
            content=response_text,
            backend=backend,
            source=source,
            archive_level=archive_level,
            created_at=created_at,
        ),
    ]

    for index, tool_call in enumerate(tool_calls, start=1):
        events.append(
            _tool_event(
                sequence=index,
                session_id=session_id,
                request_id=request_id,
                run_id=run_id,
                task_id=task_id,
                backend=backend,
                tool_call=tool_call,
                source=source,
                archive_level=archive_level,
                created_at=created_at,
            )
        )

    return events


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
    limit = _PREVIEW_LIMITS[_normalize_archive_level(archive_level)]
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _event_id(payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"raw:{digest[:32]}"


def _content_hash(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_display_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"), default=str)


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _first_bool(payload: Mapping[str, Any], *keys: str) -> bool | None:
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
    status = _first_text(payload, "status")
    if status:
        return status
    if tool_success is True:
        return "ok"
    if tool_success is False:
        return "error"
    return "unknown"
