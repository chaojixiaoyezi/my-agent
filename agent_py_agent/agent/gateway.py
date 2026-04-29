from __future__ import annotations

"""LLM: compatibility facade for the split `agent.gateway_parts` modules.

给人看的解释：
真正的 gateway 实现已经拆到 `agent_py_agent.agent.gateway_parts` 目录。
这个文件保留老导入路径，避免 `from agent_py_agent.agent.gateway import ...` 的调用方失效。
新代码可以优先按职责导入 gateway_parts 里的具体模块。
"""

from .gateway_parts import *  # noqa: F401,F403
