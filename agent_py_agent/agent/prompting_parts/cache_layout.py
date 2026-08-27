from __future__ import annotations

"""Provider-neutral prompt layout for append-only native prompt caching.

The runtime still sees one ordinary string.  Typed attributes let a provider
adapter keep stable system text and a run-stable initial user message ahead of
append-only native history, while placing request-varying facts at the tail.
"""

from dataclasses import dataclass


# LLM: This immutable projection is the only structured description of native prompt ordering;
# adapters may relocate typed parts but must send all three without parsing prose.
# 类用途: 保存稳定 system、稳定首条用户消息和动态尾部，避免靠标题或自然语言猜缓存边界。
@dataclass(frozen=True)
class PromptCacheLayout:
    stable_prefix: str
    volatile_suffix: str
    stable_user_prefix: str = ""

    # LLM: Rendering must be lossless because text backends and archives consume the same full
    # string even when they ignore provider-specific message placement.
    # 函数用途: 按模型原本应看到的顺序还原完整 prompt。
    def render(self) -> str:
        return _join_prompt_parts(
            self.stable_prefix,
            self.stable_user_prefix,
            self.volatile_suffix,
        )


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
        *,
        stable_user_prefix: str = "",
    ) -> CacheStructuredPrompt:
        layout = PromptCacheLayout(
            stable_prefix=str(stable_prefix or ""),
            volatile_suffix=str(volatile_suffix or ""),
            stable_user_prefix=str(stable_user_prefix or ""),
        )
        value = super().__new__(cls, layout.render())
        value.cache_layout = layout
        return value


# LLM: Joining rules are shared by the string value and tests; never inject a sentinel that could
# leak into provider input or archives.
# 函数用途: 用两个换行连接所有非空 typed 段落，并正确处理空段。
def _join_prompt_parts(*parts: str) -> str:
    return "\n\n".join(str(part) for part in parts if str(part))


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
