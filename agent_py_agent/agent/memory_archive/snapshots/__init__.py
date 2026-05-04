from __future__ import annotations

"""LLM: lightweight recovery snapshot builder for run/gateway/subagent completion points.

新手说明:
这个文件专门负责写"恢复锚点"。
它不保存大段工具输出，只保存用户意图、助手动作、工具摘要、任务/请求 ID、恢复路径和 token 估算。
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..models import CompressionSnapshot, utc_now_iso
from ._helpers import (
    _content_hash,
    _dedupe_texts,
    _normalize_archive_level,
    _participants,
    _preview,
    _snapshot_id,
    _stable_json,
    _tool_snapshot,
)
from ..storage import append_snapshot, write_compression_snapshot_file
from ..tokens import estimate_tokens


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


@dataclass(frozen=True)
class CompressionSnapshotInput:
    """Input bundle for write_compression_snapshot and on_before_compression."""
    session_id: str
    turn_id: str
    role: str
    content: str
    archive_level: int = 3
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "compression"
    backend: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    content_paths: Iterable[str] | None = None
    task_refs: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class RecoverySnapshotInput:
    """Input bundle for write_recovery_snapshot."""
    session_id: str
    user_prompt: str
    response_text: str
    backend: str
    source: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    status: str = "ok"
    error_code: str = ""
    tool_calls: Iterable[Mapping[str, Any]] | None = None
    task_refs: Iterable[str] | None = None
    content_paths: Iterable[str] | None = None
    next_actions: Iterable[str] | None = None
    archive_level: int = 3
    created_at: str | None = None


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
    snapshot_id = _make_recovery_snapshot_id(timestamp, session_id, request_id, run_id, task_id, source, status, user_prompt, response_text)
    snapshot = _build_recovery_snapshot(
        snapshot_id=snapshot_id,
        session_id=session_id,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        source=source,
        status=status,
        error_code=error_code,
        backend=backend,
        normalized_tools=normalized_tools,
        user_prompt=user_prompt,
        response_text=response_text,
        level=level,
        token_estimate=token_estimate,
        clean_task_refs=clean_task_refs,
        clean_next_actions=clean_next_actions,
        clean_content_paths=clean_content_paths,
        timestamp=timestamp,
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
    snapshot = _build_compression_snapshot(
        snapshot_id=snapshot_id,
        session_id=session_id,
        turn_id=turn_id,
        source=source,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        backend=backend,
        role=role,
        normalized_tools=normalized_tools,
        token_estimate=token_estimate,
        level=level,
        clean_task_refs=_dedupe_texts([run_id, task_id, *(task_refs or [])]),
        clean_next_actions=_dedupe_texts(next_actions or []),
        clean_content_paths=_dedupe_texts(content_paths or []),
        content=content,
        timestamp=timestamp,
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


# ---- internal helpers (build functions only, not public API) ----

def _make_recovery_snapshot_id(
    timestamp: str,
    session_id: str,
    request_id: str,
    run_id: str,
    task_id: str,
    source: str,
    status: str,
    user_prompt: str,
    response_text: str,
) -> str:
    """Build snapshot ID for recovery snapshots."""
    return _snapshot_id(
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


def _build_recovery_snapshot(
    *,
    snapshot_id: str,
    session_id: str,
    request_id: str,
    run_id: str,
    task_id: str,
    source: str,
    status: str,
    error_code: str,
    backend: str,
    normalized_tools: list[dict[str, Any]],
    user_prompt: str,
    response_text: str,
    level: int,
    token_estimate: int,
    clean_task_refs: list[str],
    clean_next_actions: list[str],
    clean_content_paths: list[str],
    timestamp: str,
) -> CompressionSnapshot:
    """Build a CompressionSnapshot for recovery snapshot."""
    return CompressionSnapshot(
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


def _build_compression_snapshot(
    *,
    snapshot_id: str,
    session_id: str,
    turn_id: str,
    source: str,
    request_id: str,
    run_id: str,
    task_id: str,
    backend: str,
    role: str,
    normalized_tools: list[dict[str, Any]],
    token_estimate: int,
    level: int,
    clean_task_refs: list[str],
    clean_next_actions: list[str],
    clean_content_paths: list[str],
    content: str,
    timestamp: str,
) -> CompressionSnapshot:
    """Build a CompressionSnapshot for compression snapshot."""
    return CompressionSnapshot(
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
        task_refs=clean_task_refs,
        next_actions=clean_next_actions,
        token_usage={"estimate": token_estimate, "archive_level": level, "backend": backend},
        archive_level=level,
        content_paths=clean_content_paths,
        turn_id=str(turn_id),
        role=str(role or "system"),
        content=_preview(content, level),
        token_estimate=token_estimate,
        timestamp=timestamp,
        created_at=timestamp,
    )