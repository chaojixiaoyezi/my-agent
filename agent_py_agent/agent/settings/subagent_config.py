"""subagent user-facing defaults."""

# LLM: Keep this mirror aligned with the small user-facing subagent config surface.
# 模块用途: 子代理公开配置模型；旧版细碎限制不再作为默认用户配置入口。

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SubagentConfig"]


# LLM: SubagentConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: SubagentConfig 配置模型，保存 配置系统 的默认值和可调参数。
@dataclass
class SubagentConfig:
    """Subagent user-facing switches and directories."""

    enable_subagents: bool = True
    subagent_mode: str = "trusted_local_hardening"
    max_subagents: int = 1000
    subagent_workspace: str = "data/subagents"
    subagent_role_template_dirs: list[str] = field(default_factory=list)
    subagent_debug_trace_level: int = 0
    acceptance_execute_tests: bool = False
    acceptance_test_timeout_seconds: int = 120
