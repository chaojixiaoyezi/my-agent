# LLM: This module owns only Anthropic prompt-cache breakpoint projection. It must preserve
# caller-owned messages/tools, keep provider ordering unchanged, and never decide compaction.
# 模块用途: 为 Anthropic 兼容的原生多轮请求标记稳定缓存边界，降低长工具循环反复发送旧上下文的成本。

from __future__ import annotations

from typing import Any

_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}
_CACHEABLE_CONTENT_TYPES = frozenset({"text", "tool_use", "tool_result"})


# LLM: This is the only Anthropic message projection selector. Disabled and ordinary text paths
# must retain the legacy payload shape; enabled native paths delegate to cache breakpoint logic.
# 函数用途: 按配置选择旧消息形态或主动缓存形态，让 backend 请求组装保持单一入口。
def anthropic_messages_with_optional_cache(
    *,
    prompt: str,
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]],
    cache_enabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if cache_enabled and messages is not None:
        return anthropic_native_payload_with_cache(
            prompt=prompt,
            messages=messages,
            tools=tools,
        )
    if messages:
        prepared_messages = (
            [{"role": "user", "content": prompt}, *messages]
            if prompt
            else messages
        )
        return prepared_messages, tools
    return [{"role": "user", "content": prompt}], tools


# LLM: The returned payload uses at most three host-owned breakpoints: final tool, immutable
# initial prompt, and newest cacheable history block. Inputs are copy-on-write and stay untouched.
# 函数用途: 给一次原生多轮请求的工具、固定提示和最新历史增加主动缓存断点。
def anthropic_native_payload_with_cache(
    *,
    prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not prompt and not messages:
        return [{"role": "user", "content": ""}], _mark_final_tool(tools)
    prepared_messages = _prompt_prefixed_messages(prompt, messages)
    if messages:
        _mark_latest_cacheable_history_block(
            prepared_messages,
            history_start=1 if prompt else 0,
        )
    return prepared_messages, _mark_final_tool(tools)


# LLM: The original task prompt is the immutable first native user turn. Represent it as an
# Anthropic text block so cache_control is protocol-valid without changing its text or position.
# 函数用途: 保留首条真实用户提示的位置，并把它作为长期稳定的缓存前缀。
def _prompt_prefixed_messages(
    prompt: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not prompt:
        return list(messages)
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
    "anthropic_messages_with_optional_cache",
    "anthropic_native_payload_with_cache",
]
