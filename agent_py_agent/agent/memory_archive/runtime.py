from __future__ import annotations

"""LLM: build and append raw archive events for one SimpleAgent run turn.

新手说明:
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

    新手说明:
    调用方拿到这个结果后，可以知道这轮写到了哪些文件、写了几条事件、粗略占多少 token。
    里面也带回 event_id 和 content_hash，方便测试、排障或后续把归档记录和请求日志串起来。

    字段说明:
    `write_paths` 是实际写入过的 JSONL 文件；`event_count` 是事件数量；
    `token_estimate` 是本轮粗略 token 估算；`event_ids` 和 `content_hashes` 方便追踪和去重；
    `events` 是已经构造并写入的事件对象，测试和 doctor 可以直接检查。
    """

    write_paths: tuple[Path, ...]
    event_count: int
    token_estimate: int
    event_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    events: tuple[RawMemoryEvent, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        """LLM: compatibility alias for callers that expect `paths`.

        新手说明:
        早期调用方可能只知道 `paths` 这个短名字；这里返回同一个 `write_paths`，避免破坏旧代码。

        返回说明:
        返回实际写入过的 JSONL 路径元组。
        """

        return self.write_paths

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize the archive result without losing event details.

        新手说明:
        如果 CLI 或 doctor 想把这次归档结果打印成 JSON，可以直接用这个方法。
        路径会转成字符串，事件会转成普通字典。

        返回说明:
        返回 JSON 友好的结果字典。
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
        """LLM: provide dict-like access for compatibility with older tests or callers.

        新手说明:
        有些代码可能写 `result["event_count"]`，有些写 `result.event_count`。
        这个方法让两种写法都能工作，但真实字段仍然由 dataclass 管理。

        参数说明:
        `key` 是 `to_dict()` 输出里的字段名。

        返回说明:
        返回对应字段值；字段不存在时按普通 dict 行为抛出 `KeyError`。
        """

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

    新手说明:
    真实 `SimpleAgent.run()` 后面只要把本轮已经知道的信息丢进来，就能得到统一格式的冷归档。
    这里不会保存 system prompt 或内置 prompt，也不会把长正文写成 blob；目前只保留短预览、hash 和空的正文路径占位。

    参数说明:
    `root` 是工作区根目录；`session_id` 标识会话；`user_prompt` 和 `response_text` 是本轮用户输入和助手输出。
    `backend` 记录使用的模型后端；`tool_calls` 是本轮工具调用元数据。
    `request_id`、`run_id`、`task_id` 用于把事件串回请求、运行和任务。
    `source` 标识来源；`archive_level` 控制预览长度；`created_at` 可固定事件时间，便于测试。

    返回说明:
    返回 `ArchiveRunTurnResult`，包含写入路径、事件数、hash 和事件对象。

    副作用说明:
    会向 `memory/raw/YYYY-MM-DD.jsonl` 追加 raw event，并由 storage 层读回校验。
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

    新手说明:
    这一步只拼事件，不碰磁盘。
    先放用户消息，再放助手回答，再按原顺序放工具元数据，后面测试稳定性也主要看这里。

    参数说明:
    所有参数都是一轮 run 的已知事实；`tool_calls` 必须已经归一化成字典列表。

    返回说明:
    返回按写入顺序排列的 `RawMemoryEvent` 列表。
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
    """LLM: create one user or assistant message archive event.

    新手说明:
    用户消息和助手回复字段形状基本一样，只是 speaker、target 和 action 不同。
    这里统一生成 event_id、短预览和内容 hash，避免两边格式漂移。

    参数说明:
    `sequence` 是本轮内顺序号；`session_id`、`request_id`、`run_id`、`task_id` 是追踪编号。
    `speaker` 是说话方；`target` 是接收方；`action` 是动作类型；`content` 是要摘要的正文。
    `backend`、`source`、`archive_level`、`created_at` 进入事件元数据。

    返回说明:
    返回尚未写盘的 `RawMemoryEvent`。
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

    参数说明:
    `sequence` 是工具调用在本轮中的顺序；`tool_call` 是归一化后的工具调用字典；
    其他编号、backend、source、archive_level 和 created_at 都用于追踪和归档。

    返回说明:
    返回一条 action 为 `tool_call` 的 `RawMemoryEvent`。
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

    参数说明:
    `tool_call` 是原始工具元数据；命名参数是已经提取好的常用字段。

    返回说明:
    返回可序列化字典，供 `_tool_event()` 生成预览和 hash。
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

    参数说明:
    `call` 是一个工具调用描述，类型可能不固定。

    返回说明:
    返回普通字典。
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

    参数说明:
    `value` 是用户配置、CLI 或调用方传入的归档级别。

    返回说明:
    返回 0、1、2、3 之一。
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

    参数说明:
    `content` 是原始文本；`archive_level` 决定最大保留字符数。

    返回说明:
    返回原文或带 `...` 的截断预览。
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

    参数说明:
    `payload` 是决定事件身份的关键字段，不一定是完整事件。

    返回说明:
    返回 `raw:<digest>` 格式的短 ID。
    """

    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"raw:{digest[:32]}"


def _content_hash(content: str) -> str:
    """LLM: compute a SHA-256 content hash with an explicit prefix.

    新手说明:
    hash 让我们以后能确认正文是否变化，而不用把完整正文都放在索引里。

    参数说明:
    `content` 是要做摘要的文本。

    返回说明:
    返回 `sha256:<hex>` 字符串。
    """

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _canonical_json(payload: Any) -> str:
    """LLM: serialize payloads into deterministic JSON for identity hashes.

    新手说明:
    字典字段顺序不同不应该影响事件 ID。排序后的 JSON 能保证 hash 稳定。

    参数说明:
    `payload` 可以是 dict、list 或其他能被 JSON 默认处理的对象。

    返回说明:
    返回稳定 JSON 字符串。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_display_json(payload: Any) -> str:
    """LLM: serialize metadata for human-facing previews while keeping input order.

    新手说明:
    展示预览更希望保留人工构造的字段顺序，所以这里不排序。

    参数说明:
    `payload` 是要展示的工具元数据。

    返回说明:
    返回紧凑 JSON 字符串。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"), default=str)


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """LLM: find the first non-empty text value among candidate keys.

    新手说明:
    不同工具后端可能把同一个概念叫 `tool_name`、`tool` 或 `name`。
    这个 helper 按优先级找第一个可用字段。

    参数说明:
    `payload` 是工具元数据；`keys` 是候选字段名。

    返回说明:
    返回第一个非空字符串；都没有时返回空字符串。
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

    参数说明:
    `payload` 是工具元数据；`keys` 是候选字段名。

    返回说明:
    返回布尔值；无法判断时返回 `None`。
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

    参数说明:
    `payload` 是工具元数据；`tool_success` 是已经归一化的成功布尔值。

    返回说明:
    返回 `ok`、`error`、`unknown` 或工具自带状态字符串。
    """

    status = _first_text(payload, "status")
    if status:
        return status
    if tool_success is True:
        return "ok"
    if tool_success is False:
        return "error"
    return "unknown"
