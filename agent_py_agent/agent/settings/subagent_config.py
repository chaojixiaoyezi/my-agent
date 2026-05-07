"""subagent limits and policies."""

# LLM: 默认值会改变任务拆分和自动执行强度，调参需同步子代理测试。
# 模块用途: 子代理数量、并发、自动化和验收策略配置模型。

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SubagentConfig"]


# LLM: SubagentConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentConfig 配置模型，保存 配置系统 的默认值和可调参数。
@dataclass
class SubagentConfig:
    """Subagent limits and automation policies."""

    enable_subagents: bool = True
    max_subagents: int = 5
    subagent_workspace: str = "data/subagents"
    subagent_workflow_mode: str = "auto"
    subagent_builtin_workflows: bool = True
    subagent_user_workflow_dirs: list[str] = field(default_factory=lambda: [".agent/workflows/user"])
    subagent_workflow_review_rounds: int = 1
    subagent_workflow_config_warnings: list[dict[str, object]] = field(default_factory=list)
    task_max_subagents: int = 0
    task_max_grandchildren: int = 0
    subagent_automation_level: int = 2
    dynamic_timeout_safety_margin: float = 2.0
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 600
    max_auto_split_depth: int = 2
    max_auto_retry_attempts: int = 3