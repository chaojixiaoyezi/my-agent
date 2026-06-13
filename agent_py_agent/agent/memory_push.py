
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
    from .local_storage import LocalStore
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
        """从字符串创建 MemoryType；空值或未知值归入通用 lesson。"""
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
        # P5-2:结构化触发条件匹配提权——声明了 trigger_conditions 且与当前上下文
        # 事实匹配的记忆排到最前(软提权,不过滤未声明条件的记忆)。
        records = _prioritize_by_trigger_conditions(records, trigger_type, context)
        memories_text = _collect_memory_texts(records, trigger_type, limit)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="memory_push.search"))
    return memories_text[:limit], load_errors


# LLM: P5-2 结构化触发条件匹配(唯一消费方,守"绝不解析自然语言"铁律)。
#   conditions 是开放 dict,逐键对照 context 的结构化事实:
#   ①键值为 list → context 同键值命中任一即满足;②键名带 min_ 前缀 → context
#   对应数值 ≥ 阈值;③其余 → 字符串相等。全部声明键满足才算 matched。
#   trigger_type 键名特殊:对照本次推送的 trigger_type。匹配只做排序提权,
#   绝不淘汰未声明条件的记忆(软语义,零硬门)。
# 函数用途: 让"写明了适用场景"的教训在场景真出现时排到最前面。
def trigger_conditions_match(conditions: object, trigger_type: str, context: dict) -> bool:
    if not isinstance(conditions, dict) or not conditions:
        return False
    facts = {**(context or {}), "trigger_type": trigger_type}
    return all(_condition_satisfied(str(key), expected, facts) for key, expected in conditions.items())


# 函数用途: 单个触发条件键的判定(min_ 前缀=数值阈值,列表=任一命中,标量=相等)。
def _condition_satisfied(key: str, expected: object, facts: dict) -> bool:
    if key.startswith("min_"):
        try:
            return float(facts.get(key.removeprefix("min_")) or 0) >= float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
    actual = str(facts.get(key) or "")
    if isinstance(expected, list):
        return actual in {str(item) for item in expected}
    return actual == str(expected)


# 函数用途: 把条件匹配的记忆排到前面(稳定排序,其余相对顺序不变)。
def _prioritize_by_trigger_conditions(records: list, trigger_type: str, context: dict) -> list:
    matched: list = []
    rest: list = []
    for record in records:
        conditions = _record_trigger_conditions(record)
        (matched if trigger_conditions_match(conditions, trigger_type, context) else rest).append(record)
    return [*matched, *rest]


# 函数用途: 从记忆记录的结构化扩展位读出触发条件(没有就空)。
def _record_trigger_conditions(record) -> dict:
    attributes = getattr(record, "attributes", None)
    if not isinstance(attributes, dict):
        return {}
    conditions = attributes.get("trigger_conditions")
    return conditions if isinstance(conditions, dict) else {}


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


# LLM: 检索词构造的唯一权威。历史缺陷(B1 修复):曾写成 len(goal)>50 才把 goal
#   加进查询——中文短 goal(常态)被整个丢弃,查询只剩英文 trigger 词,中文教训
#   永远搜不到,推模式形同虚设。现在 goal 非空即入查询、超长才截断。
# 函数用途: 把触发类型和任务上下文拼成记忆检索词。
def _build_memory_query(trigger_type: str, context: dict) -> str:
    query_parts = [trigger_type]
    if context.get("task_id"):
        query_parts.append(context["task_id"])
    if context.get("failure_type"):
        query_parts.append(context["failure_type"])
    goal = str(context.get("goal", "") or "").strip()
    if goal:
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

    # 写入记忆(P5-2:trigger_conditions 作为结构化扩展字段随主事实持久化,
    # 决策端 trigger_conditions_match 按字段匹配提权,绝不解析正文)
    if ctx.trigger_conditions:
        from .memory_store.jsonl import MemoryRecord

        return memory.add_record(
            MemoryRecord(
                role=ctx.role,
                content=extended_content,
                kind=ctx.mem_type.value,
                tags=all_tags,
                attributes={"trigger_conditions": dict(ctx.trigger_conditions)},
            )
        )
    return memory.add(
        role=ctx.role,
        content=extended_content,
        kind=ctx.mem_type.value,
        tags=all_tags,
    )


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
