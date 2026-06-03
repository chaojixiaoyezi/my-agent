
from __future__ import annotations

"""compatibility facade for prompt building moved to `agent.prompting_parts`.

PromptBuilder 的真实实现已经进了 `agent_py_agent.agent.prompting_parts`。
这个文件只保留旧导入路径。
"""

from .prompting_parts import PromptBuilder, ToolSections

__all__ = ["PromptBuilder", "ToolSections"]
