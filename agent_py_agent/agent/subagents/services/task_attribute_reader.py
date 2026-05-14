from __future__ import annotations

"""Safe task attribute readers for recovery and takeover services."""

# LLM: Task attribute reader isolates mock-safe field access for recovery services.
# 模块用途: 给恢复、接管和调度策略读取 task 字段；缺字段或测试替身占位值不会被误当成真实状态。

from pathlib import Path
from typing import Any


# LLM: task_text keeps recovery logic compatible with lightweight unit-test task doubles.
# 函数用途: 安全读取 task 字符串字段；缺字段或 MagicMock 默认值按空值处理，避免误判恢复状态。
def task_text(task: Any, name: str) -> str:
    value = getattr(task, name, "")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, int | float):
        return str(value)
    return ""


# LLM: task_status normalizes task status without trusting mock-generated placeholder attributes.
# 函数用途: 返回大写状态字符串；没有真实 status 时返回空。
def task_status(task: Any) -> str:
    return task_text(task, "status").upper()


# LLM: task_role normalizes role labels for recovery strategy decisions.
# 函数用途: 返回真实 role 字符串；mock 占位属性按空角色处理。
def task_role(task: Any) -> str:
    return task_text(task, "role")


# LLM: task_int reads numeric counters defensively for old tests and partial task payloads.
# 函数用途: 缺失或非数字 runner_attempts 等字段时返回 0。
def task_int(task: Any, name: str) -> int:
    try:
        return max(0, int(getattr(task, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


# LLM: task_list extracts real list-like task fields and ignores mock placeholders.
# 函数用途: 清理 child_ids 等列表字段，保证恢复 payload 可 JSON 化且不被 MagicMock 污染。
def task_list(task: Any, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]
