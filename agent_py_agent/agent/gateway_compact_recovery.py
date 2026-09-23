# LLM: Gateway 恢复只准备一次真实请求；候选仅重投影历史及证据，原 checkpoint/CAS 成功后才安装，不复制持久历史或模型选择权威。
# 模块用途: 在原模型生成安全点执行延迟的会话 Compact，用已验证的同一请求继续发送。
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path

from .agent_core.model.context_pressure import (
    invalidate_provider_context_observation,
    projected_model_context_components,
)
from .agent_core.runtime.conversation_state import conversation_runtime_state_section
from .agent_core.runtime.loop_support import _native_provider_history_messages
from .agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    ToolLoopRequestProjection,
    project_tool_loop_request,
)
from .common.cancellation import ToolCancelled
from .conversation import history_projection
from .conversation.compact import ConversationCompactOptions, prepare_conversation_context
from .conversation.compact_guard import (
    ConversationCompactError,
    compact_exception_code,
    raise_if_compact_interrupted,
)
from .conversation.compact_projection import ConversationCompactProjection, ConversationCompactView
from .conversation.compact_provider_surface import ConversationCompactProviderSurface
from .conversation.native_history import provider_history_messages_from_rows
from .gateway_model_adoption import _request_input
from .gateway_parts import request_binding, request_context, request_prompt
from .prompting_parts.builder import PromptBuilder, PromptRenderInput, render_prepared_prompt
from .prompting_parts.cache_layout import prompt_cache_layout


# LLM: 每个候选保存自己对应的 params/输入，不从回调最后一次结果取值；成功 CAS 前只能留在内存。
# 类用途: 将候选宿主上下文、实际参数及纯投影绑定，避免回退候选和即将发送的材料错位。
@dataclass(frozen=True)
class GatewayCompactMaterial:
    conversation: request_context.GatewayConversationContext
    params: object = field(repr=False)
    request_input: ToolLoopRequestInput = field(repr=False)
    projection: ToolLoopRequestProjection = field(repr=False)


# LLM: 实例仅由原 Gateway overflow 创建；scope 已有真实执行身份，render/select 不能为其它 agent 或新请求使用该候选。
# 类用途: 等原恢复准备完成后压缩历史，再把原 CAS 已确认的材料交还同一次工具循环。
@dataclass
class GatewayCompactRecovery:
    context: object
    conversation: request_context.GatewayConversationContext
    consumed: bool = False
    prompt_input: PromptRenderInput | None = field(default=None, repr=False)
    render_params: object | None = field(default=None, repr=False)
    committed: bool = False

    # LLM: 匹配原请求及thread，不借用模型建议资格；子代理和摘要调用不消费恢复对象。
    # 函数用途: 限制延迟 Compact 只作用于本次原 Gateway 恢复轮。
    def matches(self, agent: object, params: object) -> bool:
        attrs = params.task_attributes or {}
        return (agent is self.context.agent and params.request_id == self.context.request_id
                and attrs.get("conversation_thread_id") == self.conversation.thread_id
                and params.context_scope != "task_local")

    # LLM: 自定义 builder 不支持冻结时显式拒绝完整投影；不先调用 build 再重采集，也不降级成粗估。
    # 函数用途: 在原 renderer 时点保留一次真实提示材料，仍使用原纯格式化器生成字节。
    def render(self, agent: object, params: object, request: object) -> str | None:
        if not self.matches(agent, params):
            return None
        if self.consumed:
            if not self.committed:
                raise ConversationCompactError("恢复压缩未提交，不能发送旧请求", code="COMPACT_RECOVERY_NOT_COMMITTED")
            return None
        builder = agent.prompts
        if not isinstance(builder, PromptBuilder) or type(builder).build is not PromptBuilder.build:
            raise ConversationCompactError("完整恢复提示未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        self.prompt_input = builder.prepare_render_input(request)
        self.render_params = params
        return render_prepared_prompt(self.prompt_input)

    # LLM: 进入时领取防摘要重入；准备/投影失败不发送业务，原 CAS 成功后不再调用 agent.run 或重新读取宿主材料。
    # 函数用途: 用完整当前输入选择摘要候选，提交后返回同次恢复轮的新参数与已检查提示。
    def select(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        if self.consumed or not self.matches(agent, params):
            return params, prompt
        self.consumed = True
        if self.render_params is not params or self.prompt_input is None or render_prepared_prompt(self.prompt_input) != prompt:
            raise ConversationCompactError("完整恢复输入已变化", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        source = self.conversation.compact_source
        if source is None or not source.messages:
            raise ConversationCompactError("恢复来源不完整", code="COMPACT_SOURCE_CHANGED")
        try:
            frozen = _request_input(agent, params, self.prompt_input)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConversationCompactError("完整恢复输入未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN") from exc
        if not frozen.prompt_input.native_tool_use or frozen.tool_protocol_snapshot.source_protocol != "native":
            raise ConversationCompactError("恢复工具协议未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        backend = agent.backend
        config = agent.config
        from .agent_core._tool_loop_service import _native_compact_interrupted
        from .gateway_parts.request_execution import _publish_gateway_compact_boundary

        # LLM: 复用原运行中Compact的线程与token停止语义；异常由原guard按取消处理，不新增停止权威。
        # 函数用途: 在摘要、checkpoint、CAS和返回生成前检查同一恢复轮的停止信号。
        def interrupted() -> bool:
            return _native_compact_interrupted(params)

        # LLM: 候选闭包只读取本次冻结值；不召回记忆、探针、刷新 Goal 或执行原恢复准备。
        # 函数用途: 将每个摘要候选映射为原请求材料，并沿唯一纯计量入口得到接受数字。
        def project(view: ConversationCompactView) -> ConversationCompactProjection:
            material = _project_recovery_candidate(self.context, self.conversation, params, frozen, view)
            if material.projection.status != "ready":
                raise ConversationCompactError("完整恢复投影未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
            tokens, _ = projected_model_context_components(material.projection)
            return ConversationCompactProjection(tokens, material)

        try:
            result = prepare_conversation_context(
                agent, agent.conversation_store, source.thread,
                options=ConversationCompactOptions(
                    current_prompt=params.user_prompt, exclude_request_id=self.context.request_id, force=True,
                    source=source, request_projector=project, provider_surface=_summary_surface(frozen),
                    progress_callback=request_context._gateway_compact_progress_callback(
                        self.context.on_chunk, store=agent.conversation_store, thread=source.thread,
                    ),
                    interrupt_check=interrupted,
                ),
            )
        except (InterruptedError, ToolCancelled):
            raise
        except Exception as exc:
            # 摘要的瞬时错误不能进入普通业务模型重试，否则会跳过尚未提交的恢复。
            raise ConversationCompactError("上下文压缩未完成，原始会话记录保留不变", code=compact_exception_code(exc)) from exc
        raise_if_compact_interrupted(interrupted)
        material = result.request_projection.material if result.request_projection is not None else None
        if (not result.compacted or not isinstance(material, GatewayCompactMaterial)
                or material.conversation.compact_generation != result.thread.compact_generation
                or agent.backend is not backend or agent.config is not config):
            raise ConversationCompactError("压缩后的请求材料不一致", code="COMPACT_REQUEST_PROJECTION_CHANGED")
        invalidate_provider_context_observation(material.params)
        self.conversation = material.conversation
        self.committed = True
        _publish_gateway_compact_boundary(self.context.on_chunk, result.thread.compact_generation)
        return material.params, material.projection.prompt


# LLM: 摘要继续沿原 provider surface，但只消费此次已准备的 system/schema/展示；不得重跑 Registry、推荐或 PromptBuilder。
# 函数用途: 从完整恢复输入派生摘要缓存面，保留原推荐分段，不把摘要面误当作业务容量证明。
def _summary_surface(prepared: ToolLoopRequestInput) -> ConversationCompactProviderSurface:
    layout = prompt_cache_layout(render_prepared_prompt(prepared.prompt_input))
    return ConversationCompactProviderSurface(
        layout.stable_prefix, prepared.native_tools, prepared.system_instruction,
        tuple(section for section in layout.volatile_sections if section[0] == "prompt.tool_recommendations"),
    )


# LLM: 只重投影宿主已拥有的会话注入位置和历史；Goal/记忆/工作区等值固定，正文不得用于识别来源或授权。
# 函数用途: 生成候选的完整下一请求和对应参数，所有更改都在副本，成功 CAS 前不安装。
def _project_recovery_candidate(context, conversation, params, frozen, view):
    if not view.is_candidate:
        return GatewayCompactMaterial(conversation, params, frozen, project_tool_loop_request(frozen))
    work_scope = request_binding.gateway_message_work_scope(context.request)
    rows = history_projection.conversation_history_rows(
        context.agent, view.thread_id, context.request_id, [], rows=view.messages,
        token_budget=view.history_token_budget, work_scope=work_scope,
    )
    root = str(getattr(getattr(context.agent, "home_paths", None), "owner_compact_dir", "") or "")
    if not root:
        raise ConversationCompactError("缺少原 Compact 证据地址", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    candidate = replace(
        conversation, compact_summary=view.summary, compact_generation=view.compact_generation,
        compact_operation_evidence=deepcopy(view.operation_evidence),
        recent_operation_evidence=deepcopy(view.recent_operation_evidence),
        compact_operation_evidence_ref=str(Path(root) / "conversations" / f"{view.thread_id}.jsonl"),
        history=tuple((row.role, row.content) for row in rows),
        canonical_history_messages=provider_history_messages_from_rows(rows), compact_source=None,
    )
    seed = request_prompt.gateway_conversation_history_seed(candidate, work_scope=work_scope)
    injections = list(params.runtime_injections)
    index = len(context.request.get("inject", []))
    if index >= len(injections):
        raise ConversationCompactError("缺少宿主注入位置", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    injections[index] = request_prompt._conversation_prompt_section(candidate, work_scope=work_scope, include_transcript=False)
    candidate_params = replace(params, conversation_history_seed=seed, runtime_injections=injections,
                               live_archive_state=deepcopy(params.live_archive_state))
    candidate_params = replace(candidate_params, provider_history_messages=_native_provider_history_messages(candidate_params))
    fragments = list(frozen.prompt_input.injection_fragments)
    fragments[index] = injections[index]
    prepared = replace(frozen, prompt_input=replace(frozen.prompt_input, injection_fragments=tuple(fragments)),
                       provider_history_messages=tuple(candidate_params.provider_history_messages),
                       conversation_state=conversation_runtime_state_section(candidate_params))
    return GatewayCompactMaterial(candidate, candidate_params, prepared, project_tool_loop_request(prepared))
