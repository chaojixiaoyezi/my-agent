# LLM: 这是兼容薄层，改动时优先确认下游仍从这里导入的调用方。
# 模块用途: 旧配置归一化导入路径，转发到 services 中的新实现。

from __future__ import annotations

from agent_py_agent.agent.settings.normalize import (
    normalize_agent_config,
    normalize_subagent_workflow_config,
)

__all__ = [
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]
