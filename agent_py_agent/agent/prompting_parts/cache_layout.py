from __future__ import annotations

"""Provider-neutral prompt layout for stable-prefix caching.

The runtime still sees one ordinary string.  The two typed attributes let a
provider adapter place a cache breakpoint between the byte-stable prefix and
the request-varying tail without parsing headings or user prose.
"""

from dataclasses import dataclass


# LLM: This immutable projection is the only structured description of a split prompt; provider adapters may cache the prefix but must send both parts in order.
# 类用途: 保存一次模型输入里稳定前缀和动态尾部的精确文本，避免靠标题或自然语言猜缓存边界。
@dataclass(frozen=True)
class PromptCacheLayout:
    stable_prefix: str
    volatile_suffix: str

    # LLM: Rendering must be lossless because text backends and archives consume the same full string even when they ignore cache metadata.
    # 函数用途: 按模型原本应看到的顺序还原完整 prompt。
    def render(self) -> str:
        return _join_prompt_parts(self.stable_prefix, self.volatile_suffix)


# LLM: This string subclass carries cache metadata only in memory; JSON serialization and ordinary string consumers must receive the exact full prompt.
# 类用途: 让现有后端、归档和 token 统计继续把 prompt 当普通字符串，同时给兼容后端一个结构化缓存边界。
class CacheStructuredPrompt(str):
    cache_layout: PromptCacheLayout

    # LLM: Construct the string value and attached immutable layout atomically so neither surface can drift from the other.
    # 函数用途: 创建带缓存布局的完整 prompt 字符串。
    def __new__(
        cls,
        stable_prefix: str,
        volatile_suffix: str,
    ) -> CacheStructuredPrompt:
        layout = PromptCacheLayout(
            stable_prefix=str(stable_prefix or ""),
            volatile_suffix=str(volatile_suffix or ""),
        )
        value = super().__new__(cls, layout.render())
        value.cache_layout = layout
        return value


# LLM: Joining rules are shared by the string value and tests; never inject a sentinel that could leak into provider input or archives.
# 函数用途: 用两个换行连接非空段落，并正确处理任一侧为空的情况。
def _join_prompt_parts(stable_prefix: str, volatile_suffix: str) -> str:
    if stable_prefix and volatile_suffix:
        return f"{stable_prefix}\n\n{volatile_suffix}"
    return stable_prefix or volatile_suffix


# LLM: Provider adapters read only this typed attribute and never infer stability from headings, content, or user language.
# 函数用途: 取出 prompt 自带的缓存布局；普通字符串没有布局时返回 None。
def prompt_cache_layout(prompt: object) -> PromptCacheLayout | None:
    layout = getattr(prompt, "cache_layout", None)
    return layout if isinstance(layout, PromptCacheLayout) else None


__all__ = [
    "CacheStructuredPrompt",
    "PromptCacheLayout",
    "prompt_cache_layout",
]
