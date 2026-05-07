# LLM: Prompt-building module; keep assembled prompt sections and file-loading behavior stable.
# 模块用途: 构造系统提示、工具说明、任务上下文和会话片段。

from __future__ import annotations

"""compatibility facade for prompt building moved to `agent.prompting_parts`.

给人看的解释：
PromptBuilder 的真实实现已经进了 `agent_py_agent.agent.prompting_parts`。
这个文件只保留旧导入路径。
"""

from .prompting_parts import PromptBuilder, ToolSections

__all__ = ["PromptBuilder", "ToolSections"]
