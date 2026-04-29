from __future__ import annotations

"""LLM: compatibility facade for runtime settings moved to `agent.settings`.

给人看的解释：
真实配置加载代码已经放到 `agent_py_agent.agent.settings`。
这个文件继续提供老的 `agent.config` 导入路径。
"""

from .settings import AgentConfig, load_config, load_simple_yaml, parse_scalar

__all__ = ["AgentConfig", "load_config", "load_simple_yaml", "parse_scalar"]
