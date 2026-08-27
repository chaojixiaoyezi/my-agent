
from __future__ import annotations

"""public API for prompt construction and future prompt policy modules.

这里放'模型最终看到什么上下文'的构造逻辑。以后 prompt 压缩、上下文预算、
模板版本、工具 transcript 摘要，都应该进这个目录。
"""

from .builder import PromptBuilder, ToolSections
from .cache_layout import CacheStructuredPrompt, PromptCacheLayout, prompt_cache_layout

__all__ = [
    "CacheStructuredPrompt",
    "PromptBuilder",
    "PromptCacheLayout",
    "ToolSections",
    "prompt_cache_layout",
]
