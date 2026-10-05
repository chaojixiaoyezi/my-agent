# LLM: This module owns only Anthropic prompt-cache breakpoint projection. It must preserve
# caller-owned messages/tools, keep provider ordering unchanged, and never decide compaction.
# 模块用途: 为 Anthropic 兼容的原生多轮请求标记稳定缓存边界，降低长工具循环反复发送旧上下文的成本。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..prompting_parts.cache_layout import prompt_cache_layout

_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}
_CACHEABLE_CONTENT_TYPES = frozenset({"text", "tool_use", "tool_result"})

# 每种消息块的规范键序。历史从磁盘 transcript 读回时嵌套 dict 已按字母序（写入端 sort_keys），
# 进程内构造则是自然序；不统一会让同一条历史在重载前后发出不同字节，按前缀匹配的提示缓存
# 从第一个键序不同的块起整段失效。这里只固定**已知块的键名顺序**，不排序工具定义或 schema。
_BLOCK_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "text": ("type", "text", "cache_control"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
    "tool_use": ("type", "id", "name", "input"),
    "tool_result": ("type", "tool_use_id", "content", "is_error"),
    "image": ("type", "source"),
    "video": ("type", "source"),
}


# LLM: 只规范化历史消息块的键序，供 Anthropic 出站复用；不触碰工具定义、JSON Schema 或任何
#   调用方给的结构化输出 schema（那些顺序会进模型）。tool_use.input 是模型给的参数对象，
#   键序无语义，用与 _openai_function_call 相同的「排序往返」口径固定。
#   已知边界：tool_result.content 是块列表时**不递归**规范化，嵌套块键序保持原样；当前生产
#   构造点（message_adapter._tool_result_block 与孤儿结果 stub）恒为字符串，所以不触发重载
#   分叉；将来工具结果真的出现块列表时必须改成递归，并同步更新边界用例
#   （test_anthropic_history_normalization_does_not_recurse_into_tool_result_blocks 会变红提醒）。
# 函数用途: 让同一条历史在「同进程续跑」与「磁盘重载」两条路径上产出相同的消息块字节。
def normalize_anthropic_history_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_anthropic_message(row) for row in messages]


# LLM: 只重建已知块类型的键序；未知块类型原样返回，不猜字段、不丢内容。
# 函数用途: 单条消息的块列表规范化；content 不是列表时原样返回。
def _normalize_anthropic_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, list):
        return message
    blocks = [
        _normalize_anthropic_block(block) if isinstance(block, dict) else block
        for block in content
    ]
    return {**message, "content": blocks}


# LLM: 键序按 _BLOCK_KEY_ORDER 里的声明重建，声明外的键追加在后面并保持相对顺序，
#   保证新增字段不会被丢掉；input 与 tool_result 的嵌套结构按各自语义处理。
# 函数用途: 规范化一个 Anthropic 消息块，返回键序固定的新 dict。
def _normalize_anthropic_block(block: dict[str, Any]) -> dict[str, Any]:
    block_type = str(block.get("type") or "")
    order = _BLOCK_KEY_ORDER.get(block_type)
    if order is None:
        return block
    normalized: dict[str, Any] = {}
    for key in order:
        if key not in block:
            continue
        value = block[key]
        if key == "input" and isinstance(value, dict):
            # 与 _openai_function_call 同口径：模型给的参数对象键序无语义，排序往返固定字节。
            value = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
        normalized[key] = value
    for key, value in block.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


# LLM: This projection is the only place that may move a typed stable prefix into Anthropic's
# system blocks; the volatile prompt and caller-owned system text remain byte-for-byte intact.
# 类用途: 保存 Anthropic 请求最终使用的 system、动态 prompt 和缓存布局启用状态。
@dataclass(frozen=True)
class AnthropicPromptCacheProjection:
    system: str | list[dict[str, Any]]
    prompt: str
    stable_user_prefix: str = ""
    stable_system_cache_active: bool = False
    structured_native_layout_active: bool = False


# LLM: Any typed native layout is split into stable system and volatile adjunct even when cache
# markers are disabled, so its canonical user turn remains solely in messages. Cache-enabled
# requests additionally mark the stable system block; plain strings and text protocol stay legacy.
# 函数用途: 拆分稳定规则与动态事实；启用缓存时再给稳定 system 加供应商断点。
def anthropic_prompt_cache_projection(
    *,
    system_instruction: str,
    prompt: str,
    cache_enabled: bool,
    native_messages: bool,
) -> AnthropicPromptCacheProjection:
    layout = prompt_cache_layout(prompt)
    if not (native_messages and layout is not None and layout.stable_prefix):
        return AnthropicPromptCacheProjection(
            system=str(system_instruction or ""),
            prompt=str(prompt or ""),
        )
    if cache_enabled:
        system_blocks: str | list[dict[str, Any]] = []
        if system_instruction:
            system_blocks.append({"type": "text", "text": str(system_instruction)})
        system_blocks.append(
            {
                "type": "text",
                "text": layout.stable_prefix,
                "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
            }
        )
    else:
        system_blocks = "\n\n".join(
            text
            for text in (str(system_instruction or ""), str(layout.stable_prefix or ""))
            if text
        )
    return AnthropicPromptCacheProjection(
        system=system_blocks,
        prompt=layout.volatile_suffix,
        stable_user_prefix=layout.stable_user_prefix,
        stable_system_cache_active=bool(cache_enabled),
        structured_native_layout_active=True,
    )


# LLM: Responses 专属密文不发送给 Anthropic；除此以外这是唯一 Anthropic 消息投影入口。Disabled and ordinary text paths
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
    structured_native_layout_active: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if messages is not None:
        messages = [{**row, "content": [block for block in row["content"] if block.get("type") != "responses_reasoning"]}
                    if isinstance(row.get("content"), list) else row for row in messages]
        messages = [row for row in messages if row.get("content")]
        messages = normalize_anthropic_history_blocks(messages)
    if structured_native_layout_active and messages is not None:
        prepared_messages = _append_only_structured_messages(
            stable_user_prefix=stable_user_prefix,
            messages=messages,
            volatile_suffix=prompt,
            mark_cache=cache_enabled,
        )
        return (
            prepared_messages,
            _mark_final_tool(tools) if cache_enabled else list(tools),
        )
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


# LLM: Native structured requests receive chronological conversation/current-turn/tool IR from
# the caller, advance one message cache marker to its newest reusable prefix, then append volatile
# facts. stable_user_prefix exists only for legacy typed callers and must not duplicate IR users.
# 函数用途: 按“规范历史与当前消息→本轮动态事实”组装消息，并保持调用方历史不变。
def _append_only_structured_messages(
    *,
    stable_user_prefix: str,
    messages: list[dict[str, Any]],
    volatile_suffix: str,
    mark_cache: bool = True,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    if str(stable_user_prefix or "").strip():
        prepared.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": stable_user_prefix}],
            }
        )
    prepared.extend(messages)
    if prepared and mark_cache:
        _mark_latest_cacheable_history_block(prepared, history_start=0)
    return _append_volatile_user_text(prepared, volatile_suffix)


# LLM: Appending volatile text after the sole message marker preserves the cached prefix. Merge
# into an existing user message only copy-on-write so consecutive user messages stay provider-safe.
# 只有空白的尾部不追加：Anthropic 规范拒收空白文本块，出口校验（wire_contract）也会拦下。
# 函数用途: 把本轮变化事实接到消息尾；末条已是 user 时追加文本块，否则新建 user 消息。
def _append_volatile_user_text(
    messages: list[dict[str, Any]],
    volatile_suffix: str,
) -> list[dict[str, Any]]:
    if not str(volatile_suffix or "").strip():
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
# 只有空白的 prompt 或段落不产出文本块（Anthropic 规范拒收空白文本块，出口校验也会拦下）。
# 函数用途: 把 typed prompt 的全部段落无损投影为供应商文本块。
def _prompt_prefixed_messages(
    prompt: str,
    messages: list[dict[str, Any]],
    *,
    cache_prompt: bool = True,
) -> list[dict[str, Any]]:
    if not str(prompt or "").strip():
        return list(messages)
    if not cache_prompt:
        return [{"role": "user", "content": str(prompt)}, *messages]
    layout = prompt_cache_layout(prompt)
    if layout is not None:
        content: list[dict[str, Any]] = []
        if layout.stable_prefix.strip():
            content.append(
                {
                    "type": "text",
                    "text": layout.stable_prefix,
                    "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
                }
            )
        if layout.stable_user_prefix.strip():
            content.append(
                {
                    "type": "text",
                    "text": layout.stable_user_prefix,
                }
            )
        if layout.volatile_suffix.strip():
            content.append(
                {
                    "type": "text",
                    "text": layout.volatile_suffix,
                }
            )
        return [{"role": "user", "content": content}, *messages] if content else list(messages)
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
