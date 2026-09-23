# LLM: 共用宿主完整恢复准备与原Compact/CAS；回调只投影宿主历史，不能重跑准备、建立持久状态或改变模型。
# 模块用途: 让Gateway、子代理及后台复用一次准备、候选计量和取消失败隔离。
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace

from ..common.cancellation import ToolCancelled
from ..conversation.compact import ConversationCompactOptions, prepare_conversation_context
from ..conversation.compact_guard import (
    ConversationCompactError,
    compact_exception_code,
    raise_if_compact_interrupted,
)
from ..conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
    ConversationCompactView,
)
from ..conversation.compact_provider_surface import ConversationCompactProviderSurface
from ..prompting_parts.builder import PromptBuilder, PromptRenderInput, render_prepared_prompt
from ..prompting_parts.cache_layout import prompt_cache_layout
from .model.context_pressure import (
    invalidate_provider_context_observation,
    projected_model_context_components,
)
from .runtime.conversation_state import conversation_runtime_state_section
from .runtime.loop_support import _native_provider_history_messages
from .tool_request_capture import capture_tool_loop_request
from .tool_request_projection import ToolLoopRequestInput, ToolLoopRequestProjection


# LLM: 每个候选绑定自己的宿主上下文、参数与投影，成功CAS前只在内存，不能使用最后一个未选中候选。
# 类用途: 将候选完整请求与宿主新历史共同交回恢复轮。
@dataclass(frozen=True)
class CompactRecoveryMaterial:
    host_state: object
    params: object = field(repr=False)
    request_input: ToolLoopRequestInput = field(repr=False)
    projection: ToolLoopRequestProjection = field(repr=False)


# LLM: 仅内部宿主构造匹配和投影回调；实例绑定一次恢复，取消/失败不允许重新发送旧请求，提交沿原唯一Compact。
# 类用途: 保存真实恢复准备，在生成前执行摘要并返回已通过完整容量检查的同次请求。
@dataclass
class PreparedCompactRecovery:
    agent: object = field(repr=False)
    source: ConversationCompactSource
    host_state: object
    matches_request: Callable[[object], bool] = field(repr=False)
    project_candidate: Callable[[object, ToolLoopRequestInput, ConversationCompactView], CompactRecoveryMaterial] = field(repr=False)
    exclude_request_id: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = field(default=None, repr=False)
    interrupt_check: Callable[[], bool] | None = field(default=None, repr=False)
    on_commit: Callable[[int], object] | None = field(default=None, repr=False)
    consumed: bool = False
    prompt_input: PromptRenderInput | None = field(default=None, repr=False)
    render_params: object | None = field(default=None, repr=False)
    committed: bool = False

    # LLM: 自定义 builder 不支持冻结时显式拒绝完整投影；不先调用 build 再重采集，也不降级成粗估。
    # 函数用途: 在原 renderer 时点保留一次真实提示材料，仍使用原纯格式化器生成字节。
    def render(self, agent: object, params: object, request: object) -> str | None:
        if not (agent is self.agent and self.matches_request(params)):
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
        if self.consumed or not (agent is self.agent and self.matches_request(params)):
            return params, prompt
        self.consumed = True
        if self.render_params is not params or self.prompt_input is None or render_prepared_prompt(self.prompt_input) != prompt:
            raise ConversationCompactError("完整恢复输入已变化", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        source = self.source
        if source is None or not source.messages:
            raise ConversationCompactError("恢复来源不完整", code="COMPACT_SOURCE_CHANGED")
        try:
            frozen = capture_tool_loop_request(agent, params, self.prompt_input)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConversationCompactError("完整恢复输入未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN") from exc
        if not frozen.prompt_input.native_tool_use or frozen.tool_protocol_snapshot.source_protocol != "native":
            raise ConversationCompactError("恢复工具协议未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        backend = agent.backend
        config = agent.config
        from ._tool_loop_service import _native_compact_interrupted

        # LLM: 复用原运行中Compact的线程与token停止语义；异常由原guard按取消处理，不新增停止权威。
        # 函数用途: 在摘要、checkpoint、CAS和返回生成前检查同一恢复轮的停止信号。
        def interrupted() -> bool:
            return _native_compact_interrupted(params) or bool(self.interrupt_check and self.interrupt_check())

        # LLM: 候选闭包只读取本次冻结值；不召回记忆、探针、刷新 Goal 或执行原恢复准备。
        # 函数用途: 将每个摘要候选映射为原请求材料，并沿唯一纯计量入口得到接受数字。
        def project(view: ConversationCompactView) -> ConversationCompactProjection:
            material = self.project_candidate(params, frozen, view)
            if material.projection.status != "ready":
                raise ConversationCompactError("完整恢复投影未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
            tokens, _ = projected_model_context_components(material.projection)
            return ConversationCompactProjection(tokens, material)

        try:
            result = prepare_conversation_context(
                agent, agent.conversation_store, source.thread,
                options=ConversationCompactOptions(
                    current_prompt=params.user_prompt, exclude_request_id=self.exclude_request_id, force=True,
                    source=source, request_projector=project, provider_surface=_summary_surface(frozen),
                    progress_callback=self.progress_callback,
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
        if (not result.compacted or not isinstance(material, CompactRecoveryMaterial)
                or material.host_state.compact_generation != result.thread.compact_generation
                or agent.backend is not backend or agent.config is not config):
            raise ConversationCompactError("压缩后的请求材料不一致", code="COMPACT_REQUEST_PROJECTION_CHANGED")
        invalidate_provider_context_observation(material.params)
        self.host_state = material.host_state
        self.committed = True
        if self.on_commit is not None:
            self.on_commit(result.thread.compact_generation)
        return material.params, material.projection.prompt


# LLM: 摘要继续沿原 provider surface，但只消费此次已准备的 system/schema/展示；不得重跑 Registry、推荐或 PromptBuilder。
# 函数用途: 从完整恢复输入派生摘要缓存面，保留原推荐分段，不把摘要面误当作业务容量证明。
def _summary_surface(prepared: ToolLoopRequestInput) -> ConversationCompactProviderSurface:
    layout = prompt_cache_layout(render_prepared_prompt(prepared.prompt_input))
    return ConversationCompactProviderSurface(
        layout.stable_prefix, prepared.native_tools, prepared.system_instruction,
        tuple(section for section in layout.volatile_sections if section[0] == "prompt.tool_recommendations"),
    )




# LLM: 注入索引由宿主原组装顺序提供，不能从正文搜索；副本只替换历史/代次和该片段，其余准备与IR保持。
# 函数用途: 将一个摘要候选投影成同次请求的参数和冻结输入，供各宿主统一计量后发送。
def replace_recovery_history(params, frozen, *, history_seed, injection: str, injection_index: int):
    injections, fragments = list(params.runtime_injections), list(frozen.prompt_input.injection_fragments)
    if injection_index < 0 or injection_index >= len(injections) or injection_index >= len(fragments):
        raise ConversationCompactError("恢复注入位置未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    injections[injection_index] = fragments[injection_index] = injection
    candidate_params = replace(params, conversation_history_seed=history_seed,
                               runtime_injections=injections, live_archive_state=deepcopy(params.live_archive_state))
    candidate_params = replace(candidate_params, provider_history_messages=_native_provider_history_messages(candidate_params))
    prepared = replace(frozen, prompt_input=replace(frozen.prompt_input, injection_fragments=tuple(fragments)),
                       provider_history_messages=tuple(candidate_params.provider_history_messages),
                       conversation_state=conversation_runtime_state_section(candidate_params))
    return candidate_params, prepared
