# LLM: Task registry module; keep task lookup and metadata contracts stable.
# 模块用途: 注册、查询和更新任务元数据，给 CLI 和调度逻辑使用。

from __future__ import annotations

"""task identity decoupling module.

给人看的解释：
让 task_id 全局唯一、独立于会话，任何终端/会话都能查询。
"""

from .query import format_task_list, get_task_summary
from .registry import TaskRegistry

__all__ = ["TaskRegistry", "get_task_summary", "format_task_list"]
