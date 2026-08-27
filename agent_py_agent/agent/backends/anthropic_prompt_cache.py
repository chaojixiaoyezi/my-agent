# LLM: This module owns only Anthropic prompt-cache breakpoint projection. It must preserve
# caller-owned messages/tools, keep provider ordering unchanged, and never decide compaction.
# 模块用途: 为 Anthropic 兼容的原生多轮请求标记稳定缓存边界，降低长工具循环反复发送旧上下文的成本。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..prompting_parts.cache_layout import prompt_cache_layout

_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}
_CACHEABLE_CONTENT_TYPES = frozenset({"text", "tool_use", "tool_result"})


# LLM: This projection is the only place that may move a typed stable prefix into Anthropic's
# system blocks; the volatile prompt and caller-owned system text remain byte-for-byte intact.
# 类用途: 保存 Anthropic 请求最终使用的 system、动态 prompt 和缓存布局启用状态。
@dataclass(frozen=True)
class AnthropicPromptCacheProjection:
    system: str | list[dict[str, Any]]
    prompt: str
    stable_user_prefix: str = ""
    stable_system_cache_active: bool = False


# LLM: Only a typed CacheStructuredPrompt on a native cache-enabled request may become a cached
# system prefix. Plain strings, text protocol and disabled cache preserve their legacy shape.
# 函数用途: 把稳定规则移到供应商 system 缓存块，并让当前会话、时间和任务继续留在动态用户消息。
def anthropic_prompt_cache_projection(
    *,
    system_instruction: str,
    prompt: str,
    cache_enabled: bool,
    native_messages: bool,
) -> AnthropicPromptCacheProjection:
    layout = prompt_cache_layout(prompt)
    if not (cache_enabled and native_messages and layout is not None and layout.stable_prefix):
        return AnthropicPromptCacheProjection(
            system=str(system_instruction or ""),
            prompt=str(prompt or ""),
        )
    system_blocks: list[dict[str, Any]] = []
    if system_instruction:
        system_blocks.append({"type": "text", "text": str(system_instruction)})
    system_blocks.append(
        {
            "type": "text",
            "text": layout.stable_prefix,
            "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
        }
    )
    return AnthropicPromptCacheProjection(
        system=system_blocks,
        prompt=layout.volatile_suffix,
        stable_user_prefix=layout.stable_user_prefix,
        stable_system_cache_active=True,
    )


# LLM: This is the only Anthropic message projection selector. Disabled and ordinary text paths
# must retain the legacy payload shape; enabled native paths delegate to cache breakpoint logic.
# 函数用途: 按配置选择旧消息形态或主动缓存形态，让 backend 请求组装保持单一入口。
def anthropic_messages_with_optional_cache(
    *,
    prompt: str,
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]],
    cache_enabled: bool,
    stable_user_prefix: str = "",
    stable_system_cache_active: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if cache_enabled and messages is not None:
        return anthropic_native_payload_with_cache(
            prompt=prompt,
            messages=messages,
            tools=tools,
            stable_user_prefix=stable_user_prefix,
            stable_system_cache_active=stable_system_cache_active,
        )
    if messages:
        prepared_messages = (
            [{"role": "user", "content": prompt}, *messages]
            if prompt
            else messages
        )
        return prepared_messages, tools
    return [{"role": "user", "content": prompt}], tools


# LLM: Structured prompts cache only the stable prefix plus tools because their volatile suffix
# precedes IR history; legacy callers retain the existing latest-history breakpoint behavior.
# 函数用途: 给原生多轮请求添加可复用缓存断点，并避免把每轮变化的动态尾部反复写成无效缓存。
def anthropic_native_payload_with_cache(
    *,
    prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    stable_user_prefix: str = "",
    stable_system_cache_active: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not prompt and not messages:
        return [{"role": "user", "content": ""}], _mark_final_tool(tools)
    if stable_system_cache_active:
        prepared_messages = _append_only_structured_messages(
            stable_user_prefix=stable_user_prefix,
            messages=messages,
            volatile_suffix=prompt,
        )
    else:
        prepared_messages = _prompt_prefixed_messages(prompt, messages)
    if messages and not stable_system_cache_active and prompt_cache_layout(prompt) is None:
        _mark_latest_cacheable_history_block(
            prepared_messages,
            history_start=1 if prompt else 0,
        )
    return prepared_messages, _mark_final_tool(tools)


# LLM: Native structured requests must keep the run-stable initial user message before canonical
# IR, advance one message cache marker to the newest history prefix, then append volatile facts.
# 函数用途: 按“固定任务快照→追加工具历史→本轮事实”顺序组装消息，并保持调用方历史不变。
def _append_only_structured_messages(
    *,
    stable_user_prefix: str,
    messages: list[dict[str, Any]],
    volatile_suffix: str,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    if stable_user_prefix:
        prepared.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": stable_user_prefix}],
            }
        )
    prepared.extend(messages)
    if prepared:
        _mark_latest_cacheable_history_block(prepared, history_start=0)
    return _append_volatile_user_text(prepared, volatile_suffix)


# LLM: Appending volatile text after the sole message marker preserves the cached prefix. Merge
# into an existing user message only copy-on-write so consecutive user messages stay provider-safe.
# 函数用途: 把本轮变化事实接到消息尾；末条已是 user 时追加文本块，否则新建 user 消息。
def _append_volatile_user_text(
    messages: list[dict[str, Any]],
    volatile_suffix: str,
) -> list[dict[str, Any]]:
    if not volatile_suffix:
        return messages
    volatile_block = {"type": "text", "text": str(volatile_suffix)}
    if not messages or str(messages[-1].get("role") or "") != "user":
        return [*messages, {"role": "user", "content": [volatile_block]}]
    prepared = list(messages)
    message = prepared[-1]
    content = message.get("content")
    if isinstance(content, str):
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = list(content)
    else:
        blocks = []
    blocks.append(volatile_block)
    prepared[-1] = {**message, "content": blocks}
    return prepared


# LLM: A typed layout becomes ordered text blocks and only its leading stable block is cacheable.
# Ordinary strings retain the legacy one-block projection for third-party callers and tests.
# 函数用途: 把 typed prompt 的全部段落无损投影为供应商文本块。
def _prompt_prefixed_messages(
    prompt: str,
    messages: list[dict[str, Any]],
    *,
    cache_prompt: bool = True,
) -> list[dict[str, Any]]:
    if not prompt:
        return list(messages)
    if not cache_prompt:
        return [{"role": "user", "content": str(prompt)}, *messages]
    layout = prompt_cache_layout(prompt)
    if layout is not None:
        content: list[dict[str, Any]] = []
        if layout.stable_prefix:
            content.append(
                {
                    "type": "text",
                    "text": layout.stable_prefix,
                    "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
                }
            )
        if layout.stable_user_prefix:
            content.append(
                {
                    "type": "text",
                    "text": layout.stable_user_prefix,
                }
            )
        if layout.volatile_suffix:
            content.append(
                {
                    "type": "text",
                    "text": layout.volatile_suffix,
                }
            )
        return [{"role": "user", "content": content}, *messages]
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
                }
            ],
        },
        *messages,
    ]


# LLM: Incremental conversation caching advances only the newest provider-cacheable block.
# Copy the selected message/content/block and share all untouched history objects.
# 函数用途: 在最新一段文本、工具调用或工具结果上追加增量缓存断点，不改调用方历史。
def _mark_latest_cacheable_history_block(
    messages: list[dict[str, Any]],
    *,
    history_start: int,
) -> None:
    for message_index in range(len(messages) - 1, history_start - 1, -1):
        message = messages[message_index]
        content = message.get("content")
        if isinstance(content, str):
            if not content:
                continue
            messages[message_index] = {
                **message,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
                    }
                ],
            }
            return
        if not isinstance(content, list):
            continue
        for block_index in range(len(content) - 1, -1, -1):
            block = content[block_index]
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or ("text" if "text" in block else ""))
            if block_type not in _CACHEABLE_CONTENT_TYPES:
                continue
            copied_content = list(content)
            copied_content[block_index] = {
                **block,
                "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
            }
            messages[message_index] = {**message, "content": copied_content}
            return


# LLM: Anthropic orders cache prefixes as tools -> prompt -> messages. Marking only the final
# tool makes the whole stable tool array one prefix while preserving every caller-owned schema.
# 函数用途: 在最后一个工具定义上标缓存断点，使整份稳定工具清单可复用。
def _mark_final_tool(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        return []
    prepared = list(tools)
    prepared[-1] = {
        **prepared[-1],
        "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
    }
    return prepared


__all__ = [
    "AnthropicPromptCacheProjection",
    "anthropic_prompt_cache_projection",
    "anthropic_messages_with_optional_cache",
    "anthropic_native_payload_with_cache",
]
