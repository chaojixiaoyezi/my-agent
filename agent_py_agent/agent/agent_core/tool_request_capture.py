# LLM: 只在宿主已完成真实准备后读取原参数；未知历史不补空，不刷新工具目录、记忆或工作区。
# 模块用途: 共用正常选模与压缩恢复的完整请求捕获，纯渲染仍归 tool_request_projection。
from __future__ import annotations

from .tool_request_projection import ToolLoopRequestInput


# LLM: 只组装原事实与工具选择；seed=None仅在显式turn范围且原provider历史确知为空时合法，未知历史不补空。
# 函数用途: 将实际首请求的冻结 system、完整 IR 和原工具选择交给唯一纯投影。
def capture_tool_loop_request(agent: object, params: object, prompt_input: object) -> ToolLoopRequestInput:
    from ..conversation.models import ConversationHistorySeed
    from ..model_guidance import provider_system_instruction
    from .native_tool_protocol import model_turn_tool_choice, resolve_native_tools
    from .runtime.conversation_state import conversation_runtime_state_section
    from .tool_model_generation import _forwarded_guidance_seen

    if not isinstance(params.conversation_history_seed, ConversationHistorySeed):
        from ..conversation.compact_summary_view import AppliedCompactContext

        context = getattr(params, "compact_context", None)
        if not (params.conversation_history_seed is None and isinstance(context, AppliedCompactContext)
                and context.scope.kind == "turn" and params.provider_history_messages == []):
            raise ValueError("history_unknown")
    tools = resolve_native_tools(agent, params)
    return ToolLoopRequestInput(
        prompt_input=prompt_input, system_instruction=provider_system_instruction(agent.backend),
        tool_protocol_snapshot=params.tool_protocol_snapshot, native_tools=tuple(tools or ()),
        tool_choice=model_turn_tool_choice(params, tools), tool_ir_history=tuple(params.tool_ir_history),
        provider_history_messages=tuple(params.provider_history_messages), tool_context=tuple(params.tool_context),
        forwarded_guidance=frozenset(_forwarded_guidance_seen(params)),
        conversation_state=conversation_runtime_state_section(params),
    )
