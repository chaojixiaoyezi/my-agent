# LLM: Gateway Compact 的恢复请求需同时使用原车道身份和主代理运行展示；这里是跨 Gateway/core 的应用层编排，不建第二状态源。
# 模块用途: 为同一回合的 Compact 重载冻结最新请求展示，使模型选择和能力推荐不因第二次加载复活旧值。
from __future__ import annotations

from copy import deepcopy

from .agent_core.runtime.loop_models import RuntimeContextRequest
from .agent_core.runtime.run_params import run_params_with_request_id
from .conversation.compact_provider_surface import ConversationCompactModelSurface
from .gateway_parts import request_binding, request_context
from .tooling.tool_search_state import pending_carried_loaded_tool_names


# LLM: 身份只读原runtime_authority及原参数默认解析，禁止从carrier借用；只复制请求事实，不持有handler或创建执行权。
# 函数用途: 为同turn两次Compact上下文加载冻结最新展示与当前输入；失效回调清过的值不能被第二次加载复活。
def build_gateway_compact_load_request(context, prompt, run_params, carried_archive_tool_calls):
    authority = request_binding.gateway_runtime_authority(context.request, context.request_id)
    resolved = run_params_with_request_id(run_params)
    surface = ConversationCompactModelSurface(
        allowed_tools=tuple(resolved.allowed_tools) if resolved.allowed_tools is not None else None,
        prompt_files=tuple(resolved.prompt_files or ()),
        system_prompt_override=resolved.system_prompt_override,
        context_scope=resolved.context_scope,
        loaded_tool_names=tuple(sorted(pending_carried_loaded_tool_names(carried_archive_tool_calls))),
        presentation_context=RuntimeContextRequest(
            user_prompt=prompt, inject=[], resume_context=False, context_scope=resolved.context_scope,
            allowed_tools=deepcopy(resolved.allowed_tools), request_id=context.request_id,
            run_id=str(authority.get("run_id") or resolved.run_id),
            task_id=str(authority.get("task_id") or resolved.task_id),
            task_attributes=deepcopy(resolved.task_attributes), source="conversation_compact_summary", save=False,
        ),
        capability_presentation=resolved.capability_presentation,
        capability_presentation_turn_id=resolved.capability_presentation_turn_id,
        capability_presentation_callback=resolved.capability_presentation_callback,
    )
    return request_context.GatewayConversationLoadRequest(
        context.agent, context.request, context.request_id, prompt, context.on_chunk,
        model_surface=surface,
    )
