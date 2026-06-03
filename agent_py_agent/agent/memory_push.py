
"""记忆推送模块。

在关键决策点自动查询并注入相关记忆，实现"推模式"记忆系统。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from .local_store import LocalStore
    from .memory_store import JsonlMemory


class MemoryType(str, Enum):
    """记忆类型枚举。

    - LESSON_GENERAL: 通用教训，永不过期
    - LESSON_TASK: 任务教训，有场景限制
    - LESSON_TEMP: 临时经验，单次有效
    - CONTEXT: 上下文记忆
    - FACT: 事实"""

    LESSON_GENERAL = "lesson_general"
    LESSON_TASK = "lesson_task"
    LESSON_TEMP = "lesson_temp"
    CONTEXT = "context"
    FACT = "fact"

    @classmethod
    def from_string(cls, value: str) -> MemoryType:
        """从字符串创建 MemoryType，兼容旧记忆（无 type 标签的当作 LESSON_GENERAL）。"""
        if not value:
            return cls.LESSON_GENERAL
        normalized = value.lower().strip()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.LESSON_GENERAL


class TriggerType(str, Enum):
    """触发类型枚举。"""

    TIMEOUT = "timeout"
    FAILURE = "failure"
    PLANNING = "planning"
    GENERAL = "general"


@dataclass
class MemoryEntry:
    """记忆条目结构。"""

    type: MemoryType = MemoryType.LESSON_GENERAL
    trigger_type: str = ""
    tags: list[str] | None = None
    content: str = ""
    lesson: str = ""
    action: str = ""
    result: str = ""
    trigger_conditions: dict | None = None
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "type": self.type.value if isinstance(self.type, MemoryType) else self.type,
            "trigger_type": self.trigger_type,
            "tags": self.tags or [],
            "content": self.content,
            "lesson": self.lesson,
            "action": self.action,
            "result": self.result,
            "trigger_conditions": self.trigger_conditions or {},
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MemoryEntry:
        type_val = data.get("type", "lesson_general")
        if isinstance(type_val, str):
            mem_type = MemoryType.from_string(type_val)
        else:
            mem_type = type_val

        return cls(
            type=mem_type,
            trigger_type=data.get("trigger_type", ""),
            tags=data.get("tags"),
            content=data.get("content", ""),
            lesson=data.get("lesson", ""),
            action=data.get("action", ""),
            result=data.get("result", ""),
            trigger_conditions=data.get("trigger_conditions"),
            created_at=data.get("created_at", 0.0),
        )

    def to_memory_record_content(self) -> str:
        """转换为简短的记忆文本，供注入到上下文使用。"""
        if self.lesson:
            return f"[{self.type.value}] {self.lesson}"
        return f"[{self.type.value}] {self.content[:100]}"


def push_relevant_memories(
    agent,
    trigger_type: str,
    context: dict,
    limit: int = 3,
) -> list[str]:
    """查询并返回与当前上下文相关的记忆文本."""
    memories, _load_errors = push_relevant_memories_report(agent, trigger_type, context, limit=limit)
    return memories


def push_relevant_memories_report(
    agent,
    trigger_type: str,
    context: dict,
    limit: int = 3,
) -> tuple[list[str], list[dict]]:
    """查询相关记忆，并保留可恢复的记忆检索错误。"""
    if not hasattr(agent, "memory") or agent.memory is None:
        return [], []
    memories_text: list[str] = []
    load_errors: list[dict] = []
    try:
        query = _build_memory_query(trigger_type, context)
        records, search_errors = _search_memory_records_report(agent.memory, query, top_k=limit * 2)
        load_errors.extend(search_errors)
        memories_text = _collect_memory_texts(records, trigger_type, limit)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="memory_push.search"))
    return memories_text[:limit], load_errors


def _search_memory_records_report(memory, query: str, *, top_k: int) -> tuple[list, list[dict]]:
    search_report = getattr(memory, "search_report", None)
    if callable(search_report):
        result = search_report(query, top_k=top_k)
        if isinstance(result, tuple) and len(result) == 2:
            records, load_errors = result
            return list(records or []), list(load_errors or [])
    records = memory.search(query, top_k=top_k)
    return list(records or []), []


def _collect_memory_texts(records, trigger_type: str, limit: int) -> list[str]:
    values: list[str] = []
    for record in records:
        text = _memory_text_from_record(record, trigger_type)
        if not text:
            continue
        values.append(text)
        if len(values) >= limit:
            break
    return values


def _memory_text_from_record(record, trigger_type: str) -> str:
    if not record.content or len(record.content) < 10:
        return ""
    entry = MemoryEntry(
        type=MemoryType.from_string(record.kind),
        trigger_type=trigger_type,
        tags=record.tags or [],
        content=record.content,
        created_at=record.created_at,
    )
    return _extract_memory_text(entry, trigger_type)


def _build_memory_query(trigger_type: str, context: dict) -> str:
    """Build search query from trigger type and context."""
    query_parts = [trigger_type]
    if context.get("task_id"):
        query_parts.append(context["task_id"])
    if context.get("failure_type"):
        query_parts.append(context["failure_type"])
    goal = context.get("goal", "")
    if len(goal) > 50:
        query_parts.append(goal[:50])
    return " ".join(query_parts)


def _extract_memory_text(entry: MemoryEntry, trigger_type: str) -> str:
    """Extract and filter memory text based on trigger type."""
    desired: set[MemoryType] = {
        "timeout": {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK},
        "failure": {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK},
        "planning": {MemoryType.CONTEXT, MemoryType.LESSON_GENERAL},
    }.get(trigger_type, set())
    if not desired or entry.type not in desired:
        return ""
    text = entry.to_memory_record_content()
    if text and len(text) <= 200:
        return text
    return ""


def push_timeout_memories(agent, task_id: str, goal: str, timeout_count: int = 0) -> list[str]:
    """推送超时相关记忆。"""
    context = {
        "task_id": task_id,
        "goal": goal,
        "failure_type": "timeout",
    }
    return push_relevant_memories(agent, TriggerType.TIMEOUT.value, context, limit=3)


def push_failure_memories(agent, task_id: str, goal: str, failure_type: str) -> list[str]:
    """推送失败相关记忆。"""
    context = {
        "task_id": task_id,
        "goal": goal,
        "failure_type": failure_type,
    }
    return push_relevant_memories(agent, TriggerType.FAILURE.value, context, limit=3)


def push_planning_memories(agent, task_id: str, goal: str) -> list[str]:
    """推送计划相关记忆。"""
    context = {
        "task_id": task_id,
        "goal": goal,
    }
    return push_relevant_memories(agent, TriggerType.PLANNING.value, context, limit=3)


@dataclass
class MemoryWriteContext:
    """Context for writing a typed memory entry."""

    content: str
    mem_type: MemoryType
    trigger_type: str = ""
    tags: list[str] | None = None
    lesson: str = ""
    action: str = ""
    result: str = ""
    trigger_conditions: dict | None = None
    role: str = "system"


def write_memory_with_type(
    memory: JsonlMemory,
    ctx: MemoryWriteContext,
) -> None:
    """写入带类型标签的记忆。

    Args:
        memory: JsonlMemory 实例
        ctx: MemoryWriteContext 包含 content、mem_type 等字段"""
    # 构建扩展记忆内容（包含结构化字段）
    extended_content = ctx.content
    if ctx.lesson or ctx.action:
        parts = [ctx.content]
        if ctx.lesson:
            parts.append(f"Lesson: {ctx.lesson}")
        if ctx.action:
            parts.append(f"Action: {ctx.action}")
        if ctx.result:
            parts.append(f"Result: {ctx.result}")
        extended_content = " | ".join(parts)

    # 构建标签
    all_tags = ctx.tags or []
    if ctx.trigger_type:
        all_tags.append(ctx.trigger_type)
    if ctx.mem_type.value:
        all_tags.append(ctx.mem_type.value)

    # 写入记忆
    record = memory.add(
        role=ctx.role,
        content=extended_content,
        kind=ctx.mem_type.value,
        tags=all_tags,
    )

    return record


def format_memories_for_injection(memories: list[str]) -> str:
    """格式化记忆列表，准备注入到上下文。

    Args:
        memories: 记忆文本列表

    Returns:
        格式化的字符串，每条记忆用换行分隔"""
    if not memories:
        return ""

    lines = ["[相关记忆提示]"]
    for i, mem in enumerate(memories, 1):
        # 截取到 200 字
        if len(mem) > 200:
            mem = mem[:200] + "..."
        lines.append(f"{i}. {mem}")

    return "\n".join(lines)


__all__ = [
    "MemoryType",
    "TriggerType",
    "MemoryEntry",
    "push_relevant_memories",
    "push_relevant_memories_report",
    "push_timeout_memories",
    "push_failure_memories",
    "push_planning_memories",
    "write_memory_with_type",
    "format_memories_for_injection",
]
