# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for runtime settings moved to `agent.settings`.

给人看的解释：
真实配置加载代码已经放到 `agent_py_agent.agent.settings`。
这个文件继续提供老的 `agent.config` 导入路径。
"""

from .settings import AgentConfig, load_config, load_simple_yaml, parse_scalar

__all__ = ["AgentConfig", "load_config", "load_simple_yaml", "parse_scalar"]
