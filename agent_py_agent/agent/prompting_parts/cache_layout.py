# LLM: 缓存布局由宿主字段构造，来源分段是唯一动态正文来源；归档字符串与 provider 投影必须一致。
# 模块用途: 描述模型输入的稳定前缀和动态状态分段，不解析标题、不持有会话或权限状态。
from __future__ import annotations

"""Provider-neutral prompt layout for append-only native prompt caching.

The string value remains a complete diagnostic/archive projection. Native provider adapters
receive the canonical current user turn through structured messages, so they omit the matching
canonical user text from the prompt adjunct and place only request-varying facts last.
"""

from dataclasses import dataclass


# LLM: This immutable projection is the only structured description of native prompt ordering.
# Adapters may relocate typed parts, and may omit canonical_user_turn only when the same turn is
# already present in native messages; they must never infer boundaries by parsing prose.
# 类用途: 保存稳定前缀与带来源的动态段，整段尾部从这些字段无损生成，避免靠标题猜缓存边界。
@dataclass(frozen=True)
class PromptCacheLayout:
    stable_prefix: str
    volatile_suffix: str = ""
    stable_user_prefix: str = ""
    canonical_user_turn: str = ""
    volatile_sections: tuple[tuple[str, str], ...] = ()

    # LLM: 分段与整段只能提供一种输入；冻结分段后生成唯一整段投影，不能让两份正文各自漂移。
    # 函数用途: 检查宿主来源不为空或重复，并无损拼出诊断和摘要模型继续使用的动态正文。
    def __post_init__(self) -> None:
        sections = tuple(tuple(section) for section in self.volatile_sections)
        object.__setattr__(self, "volatile_sections", sections)
        if not sections:
            return
        if self.volatile_suffix:
            raise ValueError("动态分段与整段正文不能同时提供")
        sources: set[str] = set()
        for source, text in sections:
            if not isinstance(source, str) or not source.strip() or not isinstance(text, str):
                raise ValueError("动态分段需要非空来源字符串和正文字符串")
            if source in sources:
                raise ValueError("动态分段来源不能重复")
            sources.add(source)
        object.__setattr__(self, "volatile_suffix", _join_prompt_parts(tuple(text for _, text in sections)))

    # LLM: Rendering must be lossless because text backends and archives consume the same full
    # string even when they ignore provider-specific message placement.
    # 函数用途: 按完整诊断/归档口径还原 prompt；原生适配器会去掉 messages 中已有的当前用户副本。
    def render(self) -> str:
        return _join_prompt_parts(
            (
                self.stable_prefix,
                self.stable_user_prefix,
                self.canonical_user_turn,
                self.volatile_suffix,
            )
        )


# LLM: This string subclass carries cache metadata only in memory; JSON serialization and ordinary string consumers must receive the exact full prompt.
# 类用途: 让现有后端、归档和 token 统计继续把 prompt 当普通字符串，同时给兼容后端一个结构化缓存边界。
class CacheStructuredPrompt(str):
    cache_layout: PromptCacheLayout

    # LLM: Construct the string value and attached immutable layout atomically so neither surface can drift from the other.
    # 函数用途: 用单份动态分段或整段正文创建完整 prompt；来源只给原生 IR 去重使用，不发送权限信息。
    def __new__(
        cls,
        stable_prefix: str,
        volatile_suffix: str = "",
        *,
        stable_user_prefix: str = "",
        canonical_user_turn: str = "",
        volatile_sections: tuple[tuple[str, str], ...] = (),
    ) -> CacheStructuredPrompt:
        layout = PromptCacheLayout(
            stable_prefix=str(stable_prefix or ""),
            volatile_suffix=str(volatile_suffix or ""),
            stable_user_prefix=str(stable_user_prefix or ""),
            canonical_user_turn=str(canonical_user_turn or ""),
            volatile_sections=volatile_sections,
        )
        value = super().__new__(cls, layout.render())
        value.cache_layout = layout
        return value


# LLM: Joining rules are shared by the string value and tests; never inject a sentinel that could
# leak into provider input or archives.
# 函数用途: 用两个换行连接所有非空 typed 段落，并正确处理空段。
def _join_prompt_parts(parts: tuple[str, ...]) -> str:
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
