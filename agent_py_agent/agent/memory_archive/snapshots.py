from __future__ import annotations

"""LLM: lightweight recovery snapshot builder for run/gateway/subagent completion points.

新手说明:
这个文件专门负责写"恢复锚点"。
它不保存大段工具输出，只保存用户意图、助手动作、工具摘要、任务/请求 ID、恢复路径和 token 估算。
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from .models import CompressionSnapshot, utc_now_iso
from .storage import append_snapshot, write_compression_snapshot_file
from .tokens import estimate_tokens


SNAPSHOT_PREVIEW_LIMITS = {
    0: 2048,
    1: 1024,
    2: 512,
    3: 160,
}


class CompressionHook(Protocol):
    """LLM: protocol for external systems that need to run before compression.

    新手说明:
    任何需要在压缩前执行的逻辑（比如保存外部状态、通知监控、写审计日志）
    都可以实现这个接口并注册进来。hook 失败会阻断压缩。
    """

    def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
        """LLM: called before compression proceeds. Raise to block compression."""
        ...


_compression_hooks: list[CompressionHook] = []


def register_compression_hook(hook: CompressionHook) -> None:
    """LLM: register a callback that runs before every compression.

    新手说明:
    外部模块调用这个函数注册自己的 hook。
    hook 会在每次压缩前按注册顺序依次执行，任何一个抛异常都会阻断压缩。

    参数说明:
    `hook` 是实现了 CompressionHook 协议的可调用对象。
    """

    _compression_hooks.append(hook)


def clear_compression_hooks() -> None:
    """LLM: remove all registered compression hooks.

    新手说明:
    测试时用来清理 hook 注册表，避免测试之间互相污染。
    """

    _compression_hooks.clear()


def on_before_compression(
    root: str | Path,
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    archive_level: int = 3,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    source: str = "compression",
    backend: str = "",
    content_paths: Iterable[str] | None = None,
    task_refs: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    created_at: str | None = None,
) -> CompressionHookResult:
    """LLM: the mandatory pre-compression hook entry point.

    新手说明:
    压缩前必须调用这个函数，不能直接调 write_compression_snapshot。
    它会先执行所有注册的外部 hook，再写权威快照。
    任何一步失败都会抛异常，让上层阻断压缩流程。

    参数说明:
    参数和 write_compression_snapshot 完全一致，透传即可。

    返回说明:
    返回 CompressionHookResult；失败时直接抛出，不返回。
    """

    for hook in _compression_hooks:
        hook(session_id=session_id, turn_id=turn_id, archive_level=archive_level)
    return write_compression_snapshot(
        root,
        session_id=session_id,
        turn_id=turn_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        archive_level=archive_level,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        source=source,
        backend=backend,
        content_paths=content_paths,
        task_refs=task_refs,
        next_actions=next_actions,
        created_at=created_at,
    )


@dataclass(frozen=True)
class RecoverySnapshotResult:
    """LLM: result returned after a best-effort recovery snapshot write.

    新手说明:
    写快照成功时这里会有 snapshot_id 和 path。
    如果失败，主流程不崩，error 会说明为什么没写成。

    字段说明:
    `ok` 表示是否写入成功；`snapshot_id` 是本次快照编号；
    `path` 是成功写入的 JSONL 路径；`token_estimate` 是本轮估算 token；
    `error` 是失败时给人看的错误说明。
    """

    ok: bool
    snapshot_id: str = ""
    path: str = ""
    token_estimate: int = 0
    error: str = ""


@dataclass(frozen=True)
class CompressionHookResult:
    """LLM: result of the pre-compression hook that must succeed before compression proceeds.

    新手说明:
    和恢复快照不同，这个结果不是 best-effort。
    调用方只有在拿到它时，才能继续做真正的上下文压缩。
    """

    snapshot_id: str
    hook_path: str
    snapshot_file_path: str
    token_estimate: int
    archive_level: int


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

    新手说明:
    每轮任务结束时调用它，写一条很小的 JSONL。
    它会尽量保留恢复需要的字段，但如果磁盘或 JSONL 出问题，只返回 error，不拖死用户当前请求。

    参数说明:
    `root` 是工作区根目录；`session_id` 标识会话；`user_prompt` 和 `response_text` 是本轮输入输出。
    `backend` 是模型后端；`source` 表示来源场景；`request_id`、`run_id`、`task_id` 用来串回请求和任务。
    `status`、`error_code` 记录本轮结果；`tool_calls` 是工具摘要；`task_refs` 是相关任务引用。
    `content_paths` 指向长正文或证据文件；`next_actions` 是恢复后建议继续做的事。
    `archive_level` 控制预览长度；`created_at` 可用于测试固定时间。

    返回说明:
    返回 `RecoverySnapshotResult`；失败也不会抛出给主流程。

    副作用说明:
    成功时会向 `memory/hooks/YYYY-MM-DD.jsonl` 追加一条 snapshot。
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


def write_compression_snapshot(
    root: str | Path,
    *,
    session_id: str,
    turn_id: str,
    role: str,
    content: str,
    tool_calls: Iterable[Mapping[str, Any]] | None = None,
    archive_level: int = 3,
    request_id: str = "",
    run_id: str = "",
    task_id: str = "",
    source: str = "compression",
    backend: str = "",
    content_paths: Iterable[str] | None = None,
    task_refs: Iterable[str] | None = None,
    next_actions: Iterable[str] | None = None,
    created_at: str | None = None,
) -> CompressionHookResult:
    """LLM: write the authoritative pre-compression snapshot and the searchable hook JSONL entry.

    新手说明:
    真实压缩前必须先调用这个函数。
    它会同时写两份东西：
    1. `memory_archive/snapshots/*.json` 权威快照
    2. `memory/hooks/*.jsonl` 可搜索 hook 记录
    任意一步失败都直接抛错，让上层阻断压缩。
    """

    timestamp = created_at or utc_now_iso()
    level = _normalize_archive_level(archive_level)
    normalized_tools = [_tool_snapshot(item, level) for item in tool_calls or []]
    token_estimate = estimate_tokens(
        {
            "turn_id": turn_id,
            "role": role,
            "content": content,
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
            "turn_id": turn_id,
            "role": role,
            "source": source,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "content_hash": _content_hash(content),
        }
    )
    snapshot = CompressionSnapshot(
        snapshot_id=snapshot_id,
        session_id=str(session_id),
        compression_id=f"compression:{request_id or run_id or task_id or turn_id or snapshot_id[-8:]}",
        turn_range={
            "kind": "compression_snapshot",
            "turn_id": turn_id,
            "source": source,
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
        },
        participants=[role] if role else ["system"],
        user_intents=[_preview(content, level)] if role == "user" and content else [],
        assistant_actions=[_preview(content, level)] if role == "assistant" and content else [],
        tool_calls=normalized_tools,
        dispatch_events=[
            {
                "source": source,
                "request_id": request_id,
                "run_id": run_id,
                "task_id": task_id,
                "backend": backend,
                "status": "snapshot_written",
            }
        ],
        task_refs=_dedupe_texts([run_id, task_id, *(task_refs or [])]),
        next_actions=_dedupe_texts(next_actions or []),
        token_usage={"estimate": token_estimate, "archive_level": level, "backend": backend},
        archive_level=level,
        content_paths=_dedupe_texts(content_paths or []),
        turn_id=str(turn_id),
        role=str(role or "system"),
        content=_preview(content, level),
        token_estimate=token_estimate,
        timestamp=timestamp,
        created_at=timestamp,
    )
    snapshot_file = write_compression_snapshot_file(root, snapshot)
    hook_path = append_snapshot(root, snapshot)
    return CompressionHookResult(
        snapshot_id=snapshot_id,
        hook_path=str(hook_path),
        snapshot_file_path=str(snapshot_file),
        token_estimate=token_estimate,
        archive_level=level,
    )


def _tool_snapshot(tool_call: Mapping[str, Any], archive_level: int) -> dict[str, Any]:
    """LLM: convert a tool call record into small recovery-safe metadata.

    新手说明:
    这里只保留工具名、调用 ID、成功失败、错误码和参数预览。
    工具返回的大内容不进 hook，只保留 hash/路径这类恢复线索。

    参数说明:
    `tool_call` 是工具调用元数据；`archive_level` 控制参数预览长度。

    返回说明:
    返回适合放进 snapshot 的小字典。
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
    """LLM: derive snapshot participants from whether tools appeared.

    新手说明:
    每个快照至少包含 user 和 assistant；如果本轮有工具调用，再加 tool。

    参数说明:
    `tool_calls` 是已归一化的工具快照列表。

    返回说明:
    返回参与者名称列表。
    """

    participants = ["user", "assistant"]
    if tool_calls:
        participants.append("tool")
    return participants


def _normalize_archive_level(value: int) -> int:
    """LLM: normalize snapshot archive level into the supported 0..3 range.

    新手说明:
    级别越小，预览越长；坏值回到 3，避免快照过大。

    参数说明:
    `value` 是调用方传入的归档级别。

    返回说明:
    返回 0、1、2、3 之一。
    """

    if isinstance(value, bool):
        return 3
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 3
    return level if 0 <= level <= 3 else 3


def _preview(content: str, archive_level: int) -> str:
    """LLM: trim snapshot text fields according to archive level.

    新手说明:
    快照只放短摘要，不把完整 prompt 或工具输出塞进 hook 文件。

    参数说明:
    `content` 是原始文本；`archive_level` 决定最大长度。

    返回说明:
    返回原文或带 `...` 的截断文本。
    """

    limit = SNAPSHOT_PREVIEW_LIMITS[_normalize_archive_level(archive_level)]
    if len(content) <= limit:
        return content
    if limit <= 3:
        return content[:limit]
    return f"{content[: limit - 3]}..."


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    """LLM: keep non-empty text values once while preserving order.

    新手说明:
    task_refs、content_paths、next_actions 都可能重复，这里统一去重。

    参数说明:
    `values` 是字符串迭代器。

    返回说明:
    返回去空白、去重复后的列表。
    """

    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _snapshot_id(payload: Mapping[str, Any]) -> str:
    """LLM: derive a stable snapshot id from the key recovery facts.

    新手说明:
    同一时间、同一任务、同一内容会得到稳定 ID，方便测试和排查。

    参数说明:
    `payload` 是参与计算 ID 的关键字段。

    返回说明:
    返回 `snapshot:<digest>` 格式的 ID。
    """

    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"snapshot:{digest[:32]}"


def _content_hash(content: str) -> str:
    """LLM: compute a content hash for snapshot identity.

    新手说明:
    用 hash 代表长文本，可以判断内容是否变化，又不必把全文放进 ID。

    参数说明:
    `content` 是要摘要的文本。

    返回说明:
    返回 `sha256:<hex>` 字符串。
    """

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _stable_json(payload: Any) -> str:
    """LLM: serialize arbitrary payloads into deterministic JSON.

    新手说明:
    排序后的 JSON 能让 hash 结果稳定，不受字典字段顺序影响。

    参数说明:
    `payload` 是要序列化的对象。

    返回说明:
    返回紧凑、排序后的 JSON 字符串。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
