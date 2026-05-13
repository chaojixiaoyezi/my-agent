# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。


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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ArchiveRunTurnResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveRunTurnResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ArchiveRunTurnResult:

    write_paths: tuple[Path, ...]
    event_count: int
    token_estimate: int
    event_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    events: tuple[RawMemoryEvent, ...]

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 paths 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 paths 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
    @property
    def paths(self) -> tuple[Path, ...]:

        return self.write_paths

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:

        return {
            "write_paths": [str(path) for path in self.write_paths],
            "event_count": self.event_count,
            "token_estimate": self.token_estimate,
            "event_ids": list(self.event_ids),
            "content_hashes": list(self.content_hashes),
            "events": [event.to_dict() for event in self.events],
        }

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 __getitem__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 getitem 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
    def __getitem__(self, key: str) -> Any:

        return self.to_dict()[key]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 TurnData 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 TurnData 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class TurnData:
    """Bundle of user/assistant content for one turn."""
    user_prompt: str
    response_text: str
    tool_calls: list[dict[str, Any]]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 RunContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RunContext 的字段集合，在模块边界间传递结构化状态和结果。
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
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _build_run_turn_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build run turn events 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _build_run_turn_events(
    session_id: str,
    turn: TurnData,
    ctx: RunContext,
) -> list[RawMemoryEvent]:
    """Convert one run turn into ordered RawMemoryEvent objects without writing them."""
    return [
        *_message_events(session_id, turn, ctx),
        *_tool_events(session_id, turn, ctx),
    ]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _message_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 message events 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _message_events(session_id: str, turn: TurnData, ctx: RunContext) -> list[RawMemoryEvent]:
    """Build user and assistant message events for one turn."""
    return [
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
                preview_limits=ctx.preview_limits,
                summary_chars=ctx.summary_chars,
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
                preview_limits=ctx.preview_limits,
                summary_chars=ctx.summary_chars,
            ),
        ),
    ]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _tool_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 tool events 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _tool_events(session_id: str, turn: TurnData, ctx: RunContext) -> list[RawMemoryEvent]:
    """Build tool events for one turn."""
    events: list[RawMemoryEvent] = []
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
                    preview_limits=ctx.preview_limits,
                ),
            )
        )

    return events


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ArchiveTurnContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveTurnContext 的字段集合，在模块边界间传递结构化状态和结果。
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
    preview_limits: dict[int, int] | None = None
    summary_chars: int = 96


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ArchiveRunTurnParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ArchiveRunTurnParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ArchiveRunTurnParams:
    """Parameter bundle for archive_run_turn."""
    root: str | Path
    ctx: ArchiveTurnContext


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 archive_run_turn 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive run turn 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def archive_run_turn(
    params: ArchiveRunTurnParams,
) -> ArchiveRunTurnResult:
    """Append user, assistant, and optional tool raw archive events for one run turn."""
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
            preview_limits=ctx.preview_limits,
            summary_chars=ctx.summary_chars,
        ),
    )

    paths = _append_events(root, events)
    token_estimate = _turn_token_estimate(ctx, normalized_tool_calls)

    return ArchiveRunTurnResult(
        write_paths=tuple(paths),
        event_count=len(events),
        token_estimate=token_estimate,
        event_ids=tuple(event.event_id for event in events),
        content_hashes=tuple(event.content_hash for event in events),
        events=tuple(events),
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append events 相关记录，集中处理目标路径、格式化和状态更新。
def _append_events(root: str | Path, events: list[RawMemoryEvent]) -> list[Path]:
    paths: list[Path] = []
    for event in events:
        path = append_raw_event(root, event)
        if path not in paths:
            paths.append(path)
    return paths


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _turn_token_estimate 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 turn token estimate 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _turn_token_estimate(ctx: ArchiveTurnContext, tool_calls: list[dict[str, Any]]) -> int:
    return estimate_tokens(
        {
            "user_prompt": ctx.user_prompt,
            "response_text": ctx.response_text,
            "backend": ctx.backend,
            "tool_calls": tool_calls,
        }
    )
