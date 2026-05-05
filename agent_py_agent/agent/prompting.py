from __future__ import annotations

"""LLM: compatibility facade for prompt building moved to `agent.prompting_parts`.

给人看的解释：
PromptBuilder 的真实实现已经进了 `agent_py_agent.agent.prompting_parts`。
这个文件只保留旧导入路径。
"""

from .prompting_parts import PromptBuilder, ToolSections

__all__ = ["PromptBuilder", "ToolSections"]
