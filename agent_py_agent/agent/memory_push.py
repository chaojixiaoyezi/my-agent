"""记忆推送模块。

在关键决策点自动查询并注入相关记忆，实现"推模式"记忆系统。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .memory_store import JsonlMemory
    from .local_store import LocalStore


class MemoryType(str, Enum):
    """记忆类型枚举。

    - LESSON_GENERAL: 通用教训，永不过期
    - LESSON_TASK: 任务教训，有场景限制
    - LESSON_TEMP: 临时经验，单次有效
    - CONTEXT: 上下文记忆
    - FACT: 事实
    """

    LESSON_GENERAL = "lesson_general"
    LESSON_TASK = "lesson_task"
    LESSON_TEMP = "lesson_temp"
    CONTEXT = "context"
    FACT = "fact"

    @classmethod
    def from_string(cls, value: str) -> "MemoryType":
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
    def from_dict(cls, data: dict) -> "MemoryEntry":
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
    """查询并返回与当前上下文相关的记忆文本。

    Args:
        agent: SimpleAgent 实例
        trigger_type: 触发类型（"timeout", "failure", "planning"）
        context: 当前任务上下文，包含 task_id, goal, failure_type 等
        limit: 最多返回几条记忆

    Returns:
        记忆文本列表，每条最多 200 字
    """
    if not hasattr(agent, "memory") or agent.memory is None:
        return []

    memories_text: list[str] = []

    try:
        # 构建搜索查询
        query_parts = [trigger_type]

        # 添加上下文相关信息
        if context.get("task_id"):
            query_parts.append(context["task_id"])
        if context.get("failure_type"):
            query_parts.append(context["failure_type"])
        if context.get("goal"):
            # 从 goal 中提取关键词
            goal = context["goal"]
            if len(goal) > 50:
                query_parts.append(goal[:50])

        query = " ".join(query_parts)

        # 搜索记忆
        records = agent.memory.search(query, top_k=limit * 2)  # 多搜一些再过滤

        # 过滤和转换记忆
        for record in records:
            # 跳过太短的记录
            if not record.content or len(record.content) < 10:
                continue

            # 构建 MemoryEntry
            entry = MemoryEntry(
                type=MemoryType.from_string(record.kind),
                trigger_type=trigger_type,
                tags=record.tags or [],
                content=record.content,
                created_at=record.created_at,
            )

            # 根据触发类型过滤
            if trigger_type == "timeout":
                if entry.type in {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK}:
                    text = entry.to_memory_record_content()
                    if text and len(text) <= 200:
                        memories_text.append(text)
            elif trigger_type == "failure":
                if entry.type in {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK}:
                    text = entry.to_memory_record_content()
                    if text and len(text) <= 200:
                        memories_text.append(text)
            elif trigger_type == "planning":
                if entry.type in {MemoryType.CONTEXT, MemoryType.LESSON_GENERAL}:
                    text = entry.to_memory_record_content()
                    if text and len(text) <= 200:
                        memories_text.append(text)
            else:
                # general 查询
                text = entry.to_memory_record_content()
                if text and len(text) <= 200:
                    memories_text.append(text)

            if len(memories_text) >= limit:
                break

    except Exception:
        pass

    return memories_text[:limit]


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


def write_memory_with_type(
    memory: "JsonlMemory",
    content: str,
    mem_type: MemoryType,
    trigger_type: str = "",
    tags: list[str] | None = None,
    lesson: str = "",
    action: str = "",
    result: str = "",
    trigger_conditions: dict | None = None,
    role: str = "system",
) -> None:
    """写入带类型标签的记忆。

    Args:
        memory: JsonlMemory 实例
        content: 记忆正文
        mem_type: 记忆类型
        trigger_type: 触发类型
        tags: 标签列表
        lesson: 教训内容
        action: 建议动作
        result: 结果
        trigger_conditions: 触发条件
        role: 来源角色
    """
    # 构建扩展记忆内容（包含结构化字段）
    extended_content = content
    if lesson or action:
        parts = [content]
        if lesson:
            parts.append(f"Lesson: {lesson}")
        if action:
            parts.append(f"Action: {action}")
        if result:
            parts.append(f"Result: {result}")
        extended_content = " | ".join(parts)

    # 构建标签
    all_tags = tags or []
    if trigger_type:
        all_tags.append(trigger_type)
    if mem_type.value:
        all_tags.append(mem_type.value)

    # 写入记忆
    record = memory.add(
        role=role,
        content=extended_content,
        kind=mem_type.value,
        tags=all_tags,
    )

    return record


def format_memories_for_injection(memories: list[str]) -> str:
    """格式化记忆列表，准备注入到上下文。

    Args:
        memories: 记忆文本列表

    Returns:
        格式化的字符串，每条记忆用换行分隔
    """
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
    "push_timeout_memories",
    "push_failure_memories",
    "push_planning_memories",
    "write_memory_with_type",
    "format_memories_for_injection",
]