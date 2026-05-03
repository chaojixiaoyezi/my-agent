"""LLM: subagent limits and policies."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SubagentConfig"]


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