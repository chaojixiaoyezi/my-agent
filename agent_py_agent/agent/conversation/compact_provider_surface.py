# LLM: This module builds the cache-critical provider surface for transcript Compact from the
# same tool snapshot and PromptBuilder contracts as an ordinary model turn. It must never execute
# tools, infer cache boundaries from prose, or mutate ConversationThread state. Same-turn display
# is revalidated from host inputs; failure clears the host carrier without another decision call.
# 模块用途: 为会话压缩复用普通请求的system、工具和动态展示；历史在原读取边界隔离后直接使用，不执行工具。

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from ..agent_core.native_tool_protocol import resolve_native_tools
from ..backends.message_adapter import AnthropicMessageAdapter, iter_strip_orphaned_tool_blocks
from ..backends.tool_ir import CompactionSummary, RuntimeFactsTurn
from ..model_guidance import provider_system_instruction
from ..prompting_parts.builder import ToolSections
from ..prompting_parts.cache_layout import CacheStructuredPrompt, prompt_cache_layout
from .compact_message_source import CompactMessageSource
from .native_history import iter_provider_history_messages_from_rows

if TYPE_CHECKING:
    from ..agent_core.runtime.loop_models import RuntimeContextRequest
    from ..capability.decision_recommendation import CapabilityPresentationSelection


# LLM: 当前身份只取宿主RuntimeContextRequest，不从carrier借身份；callback仅在内存清旧值，不进结果或持久化。
# 类用途: 描述压缩与随后模型轮的同一输入面，保留None/空授权和同片展示，手动或新轮默认不继承。
@dataclass(frozen=True)
class ConversationCompactModelSurface:
    allowed_tools: tuple[str, ...] | None = None
    prompt_files: tuple[str, ...] = ()
    system_prompt_override: str | None = None
    context_scope: str = "default"
    loaded_tool_names: tuple[str, ...] = ()
    presentation_context: RuntimeContextRequest | None = None
    capability_presentation: CapabilityPresentationSelection | None = None
    capability_presentation_turn_id: str = ""
    capability_presentation_callback: Callable[[CapabilityPresentationSelection | None], object] | None = field(
        default=None, repr=False, compare=False,
    )


# LLM: 这里只保存请求展示，不含callback/handler/权限；动态段沿原typed source转原生历史，不能混进稳定前缀。
# 类用途: 保存一次压缩的稳定prompt、工具Schema、system及同片动态名卡，各候选仍使用自己的历史前缀。
@dataclass(frozen=True)
class ConversationCompactProviderSurface:
    stable_prompt_prefix: str
    tools: tuple[dict[str, Any], ...] | None
    system_instruction: str
    volatile_sections: tuple[tuple[str, str], ...] = ()


# LLM: Tool registration, protocol selection, catalog rendering, and native schemas intentionally
# reuse ordinary helpers; original preparation may synchronize tools/probe capability. Carried
# display is optional and read-only; this entry is not a pure capacity renderer or inspect path.
# 函数用途: 按原准备链冻结压缩缓存面，复核同片展示并回传采用结果；准备失败仍在摘要发送前抛出。
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
    runtime_request = model_surface.presentation_context
    if not isinstance(runtime_request, RuntimeContextRequest):
        runtime_request = RuntimeContextRequest(
            user_prompt="",
            inject=[],
            resume_context=False,
            context_scope=str(model_surface.context_scope or "default"),
            allowed_tools=allowed_tools,
            run_id=str(run_id or ""),
            source="conversation_compact_summary",
            save=False,
        )
    runtime_snapshot, protocol_snapshot = _tool_snapshots_for_run(
        agent, replace(runtime_request, allowed_tools=allowed_tools, context_scope=model_surface.context_scope),
    )
    presentation = _compact_capability_presentation(agent, model_surface, runtime_snapshot, protocol_snapshot)
    runtime_snapshot = presentation.tool_snapshot
    user_prompt = runtime_request.user_prompt if presentation.selection is not None else ""
    tool_catalog, tool_recommendations = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=agent,
            user_prompt=user_prompt,
            allowed_tools=allowed_tools,
            runtime_snapshot=runtime_snapshot,
            protocol_snapshot=protocol_snapshot,
        )
    )
    parent_prompt = agent.prompts.build(
        user_prompt,
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
            selected_skill_ids=presentation.selected_skill_ids,
            required_skill_ids=presentation.required_skill_ids,
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
        volatile_sections=tuple(
            section for section in layout.volatile_sections
            if presentation.selection is not None and section[0] == "prompt.tool_recommendations"
        ),
    )


# LLM: 原推荐入口只在非None载体下调用；当前快照/身份/配置失效即基础面并清宿主值，绝不重决策或恢复执行权。
# 函数用途: 用当前宿主输入复核压缩前已采用的展示，沿同一内存回调通知后续重试是否继续保留。
def _compact_capability_presentation(agent, surface, runtime_snapshot, protocol_snapshot):
    from ..agent_core.runtime.loop_models import RuntimeContextRequest, RuntimeLoopParams
    from ..agent_core.runtime.loop_support import _required_action_contract_snapshot
    from ..capability.decision_recommendation import CapabilityPresentation, recommend_capabilities

    presentation = CapabilityPresentation(runtime_snapshot)
    context = surface.presentation_context
    if (surface.capability_presentation is not None and isinstance(context, RuntimeContextRequest)
            and surface.capability_presentation_turn_id):
        params = RuntimeLoopParams(
            user_prompt=context.user_prompt, root_user_prompt=context.user_prompt,
            memories=[], runtime_injections=[], routed_context=None, resume_context_section="",
            request_id=context.request_id, run_id=context.run_id, task_id=context.task_id,
            task_attributes=context.task_attributes,
            allowed_tools=list(surface.allowed_tools) if surface.allowed_tools is not None else None,
            context_scope=surface.context_scope, tool_runtime_snapshot=runtime_snapshot,
            tool_protocol_snapshot=protocol_snapshot, capability_presentation=surface.capability_presentation,
            capability_presentation_evaluated=True,
            capability_presentation_turn_id=surface.capability_presentation_turn_id,
        )
        contract = _required_action_contract_snapshot(agent, params, runtime_snapshot)
        presentation = recommend_capabilities(agent, params, runtime_snapshot, contract)
    callback = surface.capability_presentation_callback
    if callable(callback):
        callback(presentation.selection)
    return presentation


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
# canonical transcript prefix and typed current display facts. The ordinary IR adapter owns their
# provider shape; orphan repair is provider validation only and never changes canonical storage.
# 唯一可重放核心拥有相同原生转换和清扫；此公开列表接口在需要完整内存结果时物化。
# 函数用途: 返回与普通缓存面一致的完整provider数组，不改变原工具往返或动态段。
def conversation_compact_provider_messages(
    previous_summary: str,
    compact_generation: int,
    rows: Iterable[object],
    *,
    volatile_sections: tuple[tuple[str, str], ...] = (),
) -> list[dict[str, Any]]:
    return list(conversation_compact_provider_source(previous_summary, compact_generation, rows,
                                                     volatile_sections=volatile_sections))


# LLM: 各遍重放同一rows，原生最后信封与orphan规则共用原实现；不缓存完整provider数组，不授予历史覆盖。
# 函数用途: 为摘要计量与分段提供同一可重放缓存面，完整旧摘要和展示段仍原样保留。
def conversation_compact_provider_source(previous_summary, compact_generation, rows, *, volatile_sections=()):
    rows = rows if isinstance(rows, Sequence) else tuple(rows)
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
    current_display = AnthropicMessageAdapter().to_provider_messages(
        [RuntimeFactsTurn(text=text, source=source) for source, text in volatile_sections]
    )
    # LLM: 每次重放都沿同一冻结rows，yield from向native迭代器传播提前关闭，不能留下文件句柄。
    # 函数用途: 在原生历史前后加原摘要和展示消息，保持原数组顺序。
    def replay():
        yield from prefix
        yield from iter_provider_history_messages_from_rows(rows)
        yield from current_display

    return CompactMessageSource(lambda: iter_strip_orphaned_tool_blocks(replay))


__all__ = [
    "ConversationCompactModelSurface",
    "ConversationCompactProviderSurface",
    "conversation_compact_provider_messages",
    "conversation_compact_provider_prompt",
    "prepare_conversation_compact_provider_surface",
]
