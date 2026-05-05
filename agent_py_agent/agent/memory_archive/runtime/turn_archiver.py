"""LLM: top-level turn archiver — ArchiveRunTurnResult, archive_run_turn, and _build_run_turn_events.

给人看的解释：
这个文件放 ArchiveRunTurnResult 数据类、archive_run_turn 主函数和 _build_run_turn_events 事件拼装函数。
archive_run_turn 是 SimpleAgent.run() 完成后用来把一轮对话写入冷归档的入口。
"""

from __future__ import annotations

from collections.abc import Iterable  # noqa: F401  # re-exported for backwards compatibility
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import RawMemoryEvent, utc_now_iso
from ..storage import append_raw_event
from ..tokens import estimate_tokens
from .event_builders import (
    EventIdentity,
    MessageContext,
    ToolCallContext,
    _content_hash,
    _event_id,
    _message_event,
    _normalize_archive_level,
    _normalize_tool_call,
    _preview,
    _tool_event,
)


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
        """

        return self.write_paths

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize the archive result without losing event details.

        新手说明:
        如果 CLI 或 doctor 想把这次归档结果打印成 JSON，可以直接用这个方法。
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
        有些代码可能写 `result["event_count"]`，有些写 `result.event_count`。两种写法都能工作。
        """

        return self.to_dict()[key]


@dataclass(frozen=True)
class TurnData:
    """Bundle of user/assistant content for one turn."""
    user_prompt: str
    response_text: str
    tool_calls: list[dict[str, Any]]


@dataclass(frozen=True)
class RunContext:
    """Shared runtime fields for turn archiving."""
    backend: str
    request_id: str
    run_id: str
    task_id: str
    source: str
    archive_level: int
    created_at: str


def _build_run_turn_events(
    session_id: str,
    turn: TurnData,
    ctx: RunContext,
) -> list[RawMemoryEvent]:
    """LLM: convert one run turn into ordered RawMemoryEvent objects without writing them.

    新手说明:
    这一步只拼事件，不碰磁盘。先放用户消息，再放助手回答，再按原顺序放工具元数据。
    """

    events = [
        _message_event(
            EventIdentity(
                sequence=1,
                session_id=session_id,
                request_id=ctx.request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            ),
            MessageContext(
                speaker="user",
                target="assistant",
                action="message",
                backend=ctx.backend,
                source=ctx.source,
                archive_level=ctx.archive_level,
                created_at=ctx.created_at,
                content=turn.user_prompt,
            ),
        ),
        _message_event(
            EventIdentity(
                sequence=2,
                session_id=session_id,
                request_id=ctx.request_id,
                run_id=ctx.run_id,
                task_id=ctx.task_id,
            ),
            MessageContext(
                speaker="assistant",
                target="user",
                action="response",
                backend=ctx.backend,
                source=ctx.source,
                archive_level=ctx.archive_level,
                created_at=ctx.created_at,
                content=turn.response_text,
            ),
        ),
    ]

    for index, tool_call in enumerate(turn.tool_calls, start=1):
        events.append(
            _tool_event(
                EventIdentity(
                    sequence=index,
                    session_id=session_id,
                    request_id=ctx.request_id,
                    run_id=ctx.run_id,
                    task_id=ctx.task_id,
                ),
                ToolCallContext(
                    backend=ctx.backend,
                    tool_call=tool_call,
                    source=ctx.source,
                    archive_level=ctx.archive_level,
                    created_at=ctx.created_at,
                ),
            )
        )

    return events


@dataclass(frozen=True)
class ArchiveTurnContext:
    """Context for archiving a single turn."""
    session_id: str
    user_prompt: str
    response_text: str
    backend: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    archive_level: int = 3
    created_at: str | None = None


@dataclass(frozen=True)
class ArchiveRunTurnParams:
    """Parameter bundle for archive_run_turn."""
    root: str | Path
    ctx: ArchiveTurnContext


def archive_run_turn(
    params: ArchiveRunTurnParams,
) -> ArchiveRunTurnResult:
    """LLM: append user, assistant, and optional tool RawMemoryEvent records for one run turn.

    新手说明:
    真实 `SimpleAgent.run()` 后面只要把本轮已经知道的信息丢进来，就能得到统一格式的冷归档。
    这里不会保存 system prompt 或内置 prompt，也不会把长正文写成 blob；目前只保留短预览、hash 和空的正文路径占位。

    参数说明:
    `root` 是工作区根目录；`ctx` 包含 session_id、user_prompt、response_text 等归档所需字段。
    `ctx.tool_calls` 是本轮工具调用元数据；`ctx.request_id`、`ctx.run_id`、`ctx.task_id` 用于把事件串回请求、运行和任务。
    `ctx.source` 标识来源；`ctx.archive_level` 控制预览长度；`ctx.created_at` 可固定事件时间，便于测试。

    返回说明:
    返回 `ArchiveRunTurnResult`，包含写入路径、事件数、hash 和事件对象。

    副作用说明:
    会向 `memory/raw/YYYY-MM-DD.jsonl` 追加 raw event，并由 storage 层读回校验。
    """
    ctx = params.ctx
    root = params.root

    normalized_level = _normalize_archive_level(ctx.archive_level)
    timestamp = ctx.created_at or utc_now_iso()
    normalized_tool_calls = [_normalize_tool_call(call) for call in ctx.tool_calls or []]
    events = _build_run_turn_events(
        session_id=str(ctx.session_id),
        turn=TurnData(
            user_prompt=str(ctx.user_prompt),
            response_text=str(ctx.response_text),
            tool_calls=normalized_tool_calls,
        ),
        ctx=RunContext(
            backend=str(ctx.backend),
            request_id=str(ctx.request_id),
            run_id=str(ctx.run_id),
            task_id=str(ctx.task_id),
            source=str(ctx.source),
            archive_level=normalized_level,
            created_at=timestamp,
        ),
    )

    paths: list[Path] = []
    for event in events:
        path = append_raw_event(root, event)
        if path not in paths:
            paths.append(path)

    token_estimate = estimate_tokens(
        {
            "user_prompt": ctx.user_prompt,
            "response_text": ctx.response_text,
            "backend": ctx.backend,
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
