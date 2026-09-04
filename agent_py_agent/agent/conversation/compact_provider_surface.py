# LLM: This module builds the cache-critical provider surface for transcript Compact from the
# same tool snapshot and PromptBuilder contracts as an ordinary model turn. It must never execute
# tools, infer cache boundaries from prose, or mutate ConversationThread state.
# 模块用途: 为会话压缩复用普通请求的 system、工具和历史消息缓存前缀，摘要调用本身只有一次只读模型请求。

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from ..agent_core.native_tool_protocol import resolve_native_tools
from ..backends.message_adapter import AnthropicMessageAdapter, strip_orphaned_tool_blocks
from ..backends.tool_ir import CompactionSummary
from ..model_guidance import provider_system_instruction
from ..prompting_parts.builder import ToolSections
from ..prompting_parts.cache_layout import CacheStructuredPrompt, prompt_cache_layout
from .native_history import provider_history_messages_from_rows


# LLM: Callers provide only structured run inputs that also shape an ordinary request. The tuple
# fields preserve None-versus-empty authorization semantics and cannot be rewritten by summary text.
# 类用途: 描述一次 transcript Compact 应与随后普通模型轮保持一致的权限、prompt 文件和系统提示范围。
@dataclass(frozen=True)
class ConversationCompactModelSurface:
    allowed_tools: tuple[str, ...] | None = None
    prompt_files: tuple[str, ...] = ()
    system_prompt_override: str | None = None
    context_scope: str = "default"
    loaded_tool_names: tuple[str, ...] = ()


# LLM: This prepared value contains only immutable cache-key material. Provider history remains
# candidate-specific because Compact may try several oldest-prefix partitions before committing.
# 类用途: 保存一次压缩期间可复用的稳定 prompt 前缀、原生工具 Schema 和供应商 system 指令。
@dataclass(frozen=True)
class ConversationCompactProviderSurface:
    stable_prompt_prefix: str
    tools: tuple[dict[str, Any], ...] | None
    system_instruction: str


# LLM: Tool registration, protocol selection, catalog rendering, and native schemas intentionally
# reuse the ordinary runtime helpers. Any failure aborts Compact before provider submission rather
# than silently sending a cache-incompatible giant prompt.
# 函数用途: 按普通模型轮的同一底座合同冻结 transcript Compact 的缓存关键面。
def prepare_conversation_compact_provider_surface(
    agent: object,
    model_surface: ConversationCompactModelSurface,
    *,
    run_id: str,
) -> ConversationCompactProviderSurface:
    config = getattr(agent, "config", None)
    registry = getattr(agent, "tools", None)
    if config is None or registry is None or getattr(agent, "prompts", None) is None:
        raise RuntimeError("conversation Compact cache surface requires a complete agent runtime")
    prepare = getattr(registry, "prepare_for_run", None)
    if bool(getattr(config, "enable_tools", False)) and callable(prepare):
        prepare()

    # Import at the call boundary: loop_support imports conversation helpers during normal runtime,
    # while this module must remain importable before that runtime graph is fully initialized.
    from ..agent_core.runtime.loop_models import RuntimeContextRequest
    from ..agent_core.runtime.loop_support import (
        ToolSectionsRequest,
        _resolve_tool_sections,
        _tool_snapshots_for_run,
    )

    allowed_tools = (
        list(model_surface.allowed_tools)
        if model_surface.allowed_tools is not None
        else None
    )
    runtime_snapshot, protocol_snapshot = _tool_snapshots_for_run(
        agent,
        RuntimeContextRequest(
            user_prompt="",
            inject=[],
            resume_context=False,
            context_scope=str(model_surface.context_scope or "default"),
            allowed_tools=allowed_tools,
            run_id=str(run_id or ""),
            source="conversation_compact_summary",
            save=False,
        ),
    )
    tool_catalog, tool_recommendations = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=agent,
            user_prompt="",
            allowed_tools=allowed_tools,
            runtime_snapshot=runtime_snapshot,
            protocol_snapshot=protocol_snapshot,
        )
    )
    parent_prompt = agent.prompts.build(
        "",
        [],
        inject=[],
        prompt_files=list(model_surface.prompt_files),
        system_prompt_override=model_surface.system_prompt_override,
        context_scope=str(model_surface.context_scope or "default"),
        workspace_context_override="",
        tools=ToolSections(
            tool_catalog_section=tool_catalog,
            tool_recommendations_section=tool_recommendations,
            native_tool_use=True,
        ),
    )
    layout = prompt_cache_layout(parent_prompt)
    if layout is None or not layout.stable_prefix:
        raise RuntimeError("conversation Compact requires a structured native prompt layout")
    native_tools = resolve_native_tools(
        agent,
        SimpleNamespace(
            tool_protocol_snapshot=protocol_snapshot,
            tool_runtime_snapshot=runtime_snapshot,
            allowed_tools=allowed_tools,
            loaded_tool_names=set(model_surface.loaded_tool_names),
        ),
    )
    return ConversationCompactProviderSurface(
        stable_prompt_prefix=str(layout.stable_prefix),
        tools=(
            tuple(deepcopy(list(native_tools)))
            if native_tools is not None
            else None
        ),
        system_instruction=provider_system_instruction(getattr(agent, "backend", None)),
    )


# LLM: The synthetic request is the sole volatile suffix. Keeping it out of stable system and
# completed history lets the provider reuse the exact prefix while still seeing Compact last.
# 函数用途: 把摘要要求追加到稳定缓存面末尾，生成供应商最终接收的结构化 prompt。
def conversation_compact_provider_prompt(
    surface: ConversationCompactProviderSurface,
    instruction: str,
) -> CacheStructuredPrompt:
    return CacheStructuredPrompt(
        surface.stable_prompt_prefix,
        str(instruction or "").strip(),
    )


# LLM: Previous committed summary uses the exact ordinary-run envelope, followed by the selected
# canonical transcript prefix. Orphan repair is provider validation only and never changes storage.
# 函数用途: 按普通会话回放顺序生成待压缩历史，保留原生工具调用/结果并让摘要指令随后追加。
def conversation_compact_provider_messages(
    previous_summary: str,
    compact_generation: int,
    rows: Iterable[object],
) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    summary = str(previous_summary or "").strip()
    if summary:
        prefix = AnthropicMessageAdapter().to_provider_messages(
            [
                CompactionSummary(
                    "# Earlier Conversation Summary "
                    f"(generation {max(0, int(compact_generation or 0))})\n{summary}"
                )
            ]
        )
    canonical = [
        deepcopy(item)
        for item in provider_history_messages_from_rows(rows)
        if isinstance(item, dict)
    ]
    return strip_orphaned_tool_blocks([*prefix, *canonical])


__all__ = [
    "ConversationCompactModelSurface",
    "ConversationCompactProviderSurface",
    "conversation_compact_provider_messages",
    "conversation_compact_provider_prompt",
    "prepare_conversation_compact_provider_surface",
]
