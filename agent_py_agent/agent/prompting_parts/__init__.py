# LLM: Prompt-building module; keep assembled prompt sections and file-loading behavior stable.
# 模块用途: 构造系统提示、工具说明、任务上下文和会话片段。

from __future__ import annotations

"""public API for prompt construction and future prompt policy modules.

给人看的解释：
这里放'模型最终看到什么上下文'的构造逻辑。以后 prompt 压缩、上下文预算、
模板版本、工具 transcript 摘要，都应该进这个目录。
"""

from .builder import PromptBuilder, ToolSections

__all__ = ["PromptBuilder", "ToolSections"]
