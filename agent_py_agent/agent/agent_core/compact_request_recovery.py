# LLM: 共用宿主完整准备与原Compact/CAS；宿主投影历史，公共层合并工具交接替换，不能重跑准备或建立持久状态。
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
from ..conversation.compact_media_policy import configured_media_policy
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
from .tool_request_projection import (
    ToolLoopRequestInput,
    ToolLoopRequestProjection,
    compact_request_source_supported,
    project_tool_loop_request,
)


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
    project_active_candidate: Callable[..., CompactRecoveryMaterial] | None = field(default=None, repr=False)
    exclude_request_id: str = ""
    progress_callback: Callable[[dict[str, object]], object] | None = field(default=None, repr=False)
    interrupt_check: Callable[[], bool] | None = field(default=None, repr=False)
    on_commit: Callable[[int], object] | None = field(default=None, repr=False)
    force: bool = True
    resolved_input: ToolLoopRequestInput | None = field(default=None, repr=False)
    consumed: bool = False
    prompt_input: PromptRenderInput | None = field(default=None, repr=False)
    render_params: object | None = field(default=None, repr=False)
    committed: bool = False
    committed_thread: object | None = field(default=None, repr=False)
    # LLM: 已提交候选的（参数, prompt）；只供仍拿着渲染时原参数对象的后续尝试（瞬断重试）复用，换参数对象或回合结束即失效。
    # 字段用途: 让重试发送与首次尝试相同的已提交恢复请求，而不是压缩前的旧请求。
    committed_request: tuple[object, str] | None = field(default=None, repr=False)

    # LLM: 恢复宿主在原生成安全点独占本次Compact；普通工具轮和其它请求仍走原自动压缩。
    # 函数用途: 防止完整输入捕获前的自动压缩先推进同一来源代次。
    def owns_compact(self, agent: object, params: object) -> bool:
        return agent is self.agent and not self.consumed and self.matches_request(params)

    # LLM: 自定义 builder 不支持冻结时显式拒绝完整投影；不先调用 build 再重采集，也不降级成粗估。
    # 函数用途: 在原 renderer 时点保留一次真实提示材料，仍使用原纯格式化器生成字节。
    def render(self, agent: object, params: object, request: object) -> str | None:
        if not (agent is self.agent and self.matches_request(params)):
            return None
        if self.consumed:
            if self.resolved_input is None:
                raise ConversationCompactError("恢复压缩未提交，不能发送旧请求", code="COMPACT_RECOVERY_NOT_COMMITTED")
            return None
        builder = agent.prompts
        if not isinstance(builder, PromptBuilder) or type(builder).build is not PromptBuilder.build:
            raise ConversationCompactError("完整恢复提示未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        self.prompt_input = builder.prepare_render_input(request)
        self.render_params = params
        from .subagent.model_selection import record_first_request_prompt

        record_first_request_prompt(agent, params, request, self.prompt_input)
        return render_prepared_prompt(self.prompt_input)

    # LLM: 上下文阶段早于候选模型和回退基线；只刷新已有child首请求资格，不重新收集提示材料。
    # 函数用途: 将同次压缩后的冻结输入交给后续自动选模。
    def prepare_request(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        if self.consumed or not (agent is self.agent and self.matches_request(params)):
            return params, prompt
        prepared_params, prepared_prompt = self.select(agent, params, prompt)
        if self.resolved_input is not None and agent is self.agent and self.matches_request(params):
            from .subagent.model_selection import refresh_first_request_prompt

            refresh_first_request_prompt(agent, prepared_params, self.resolved_input)
        return prepared_params, prepared_prompt

    # LLM: 进入时领取防摘要重入；强制恢复没有消息或完整工具来源时显式拒绝，准备/投影失败不发送业务，CAS后沿同次材料发送。
    # 强制恢复遇 unknown 非文本内容（或媒体策略 off 且含媒体）报 COMPACT_REQUEST_NON_TEXT（preflight 此时只在窗口上限触发）；自动遇同类内容走 noop。
    # 已知媒体在策略不为 off 时进入压缩链，由 compact 按策略投影为归档引用或随图摘要。
    # 自动 noop 先原样返回原请求；决定摘要后解绑原参数与冻结输入的完整原生历史，失败/取消/超限收尾都不得再读它。
    # 函数用途: 用完整当前输入选择摘要候选，提交后返回同次恢复轮的新参数与已检查提示。
    def select(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        if self.consumed or not (agent is self.agent and self.matches_request(params)):
            return params, prompt
        self.consumed = True
        if self.render_params is not params or self.prompt_input is None or render_prepared_prompt(self.prompt_input) != prompt:
            raise ConversationCompactError("完整恢复输入已变化", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        source = self.source
        if source is None or (not source.messages and self.project_active_candidate is None):
            raise ConversationCompactError("恢复来源不完整", code="COMPACT_SOURCE_CHANGED")
        try:
            frozen = capture_tool_loop_request(agent, params, self.prompt_input)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConversationCompactError("完整恢复输入未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN") from exc
        if not frozen.prompt_input.native_tool_use or frozen.tool_protocol_snapshot.source_protocol != "native":
            raise ConversationCompactError("恢复工具协议未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        if self.force and not compact_request_source_supported(frozen, media_policy=configured_media_policy(agent)):
            # 结构化原因单列：无法摘要的非文本内容使压缩不可用，宿主和 TUI 据此说明为何不能继续，不与内部投影失配混码。
            raise ConversationCompactError("会话包含无法摘要的非文本内容，当前无法压缩上下文；原始记录已保留", code="COMPACT_REQUEST_NON_TEXT")
        tool_source = _recovery_tool_source(agent, source, params, frozen)
        if self.force and not source.messages and tool_source is None:
            if _recovery_tool_records_present(agent, source, params, frozen):
                # 有工具记录但身份无法证明（旧索引缺 attempt/turn、歧义或孤立）：原记录保持可见，报结构化原因而不是"没有来源"。
                raise ConversationCompactError("当前轮工具来源身份无法证明，原记录保持可见", code="COMPACT_TOOL_COVERAGE_UNKNOWN")
            raise ConversationCompactError("没有可压缩的源历史", code="COMPACT_SOURCE_EMPTY")
        if not self.force and self._automatic_noop(frozen, tool_source):
            from ._tool_loop_service import _native_compact_interrupted

            raise_if_compact_interrupted(lambda: _native_compact_interrupted(params)
                                         or bool(self.interrupt_check and self.interrupt_check()))
            self.resolved_input = frozen
            return params, prompt
        # 已决定摘要：旧请求不会再发送，候选按新种子重建历史。解绑原参数和冻结输入持有的完整原生历史，
        # 摘要期间不再驻留旧请求；只解绑这一对象，经 replace 共享同一列表的其它参数对象不受影响。
        frozen = replace(frozen, provider_history_messages=())
        object.__setattr__(params, "provider_history_messages", [])
        backend = agent.backend
        config = agent.config
        from ..memory_archive.compact_semantic_summary import semantic_summary_config

        handoff_max_chars = semantic_summary_config(agent).max_input_chars
        from ._tool_loop_service import _native_compact_interrupted

        # LLM: 复用原运行中Compact的线程与token停止语义；异常由原guard按取消处理，不新增停止权威。
        # 函数用途: 在摘要、checkpoint、CAS和返回生成前检查同一恢复轮的停止信号。
        def interrupted() -> bool:
            return _native_compact_interrupted(params) or bool(self.interrupt_check and self.interrupt_check())

        # LLM: 候选闭包只读取本次冻结值；不召回记忆、探针、刷新 Goal 或执行原恢复准备。
        # 函数用途: 将每个摘要候选映射为原请求材料，并沿唯一纯计量入口得到接受数字。
        def project(view: ConversationCompactView) -> ConversationCompactProjection:
            material = self.project_candidate(params, frozen, view)
            material = _project_mixed_recovery_material(material, view, handoff_max_chars)
            if source.compact_context is not None:
                expected = project_recovery_compact_context(source.compact_context, view)
                if getattr(material.params, "compact_context", None) != expected:
                    raise ConversationCompactError("恢复候选摘要视图不一致", code="COMPACT_REQUEST_PROJECTION_CHANGED")
            if material.projection.status != "ready":
                raise ConversationCompactError("完整恢复投影未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
            tokens, _ = projected_model_context_components(material.projection)
            return ConversationCompactProjection(tokens, material)

        try:
            if source.messages:
                result = prepare_conversation_context(
                    agent, agent.conversation_store, source.thread,
                    options=ConversationCompactOptions(
                        current_prompt=params.user_prompt, exclude_request_id=self.exclude_request_id, force=True,
                        # self.force 才是窗口上限/供应商施压；阈值自动压缩仍整段压完（force=True）但允许随图摘要。
                        pressure_forced=self.force,
                        source=source, request_projector=project, provider_surface=_summary_surface(frozen),
                        tool_source=tool_source,
                        progress_callback=self.progress_callback,
                        interrupt_check=interrupted,
                    ),
                )
            else:
                result = self._compact_active_source(params, frozen, interrupted, tool_source, handoff_max_chars)
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
        material = _committed_recovery_material(agent, source, result, material)
        invalidate_provider_context_observation(material.params)
        self.resolved_input = material.request_input
        self.host_state = material.host_state
        self.committed = True
        self.committed_thread = result.thread
        self.committed_request = (material.params, material.projection.prompt)
        if self.on_commit is not None:
            self.on_commit(result.thread.compact_generation)
        return self.committed_request

    # LLM: 已提交后，凡传入渲染时记录的那份原参数对象（按对象身份，不按请求 ID）都命中同一候选，覆盖瞬断重试。
    # 传入别的参数对象即清除：成功后循环改用候选参数，下一工具轮必然清除；宿主随回合作用域结束释放，不能跨请求复用。
    # 同轮重跑分支（空响应修复、插话取代）由 _model_turn_or_retry 先换成候选参数，不会再拿原参数进来。
    # 函数用途: 让重试直接复用已提交的恢复请求，不在旧参数上重建、回收或注入插话。
    def committed_selection(self, agent: object, params: object) -> tuple[object, str] | None:
        if self.committed_request is None or agent is not self.agent:
            return None
        if params is not self.render_params:
            self.committed_request = None
            return None
        return self.committed_request

    # LLM: 冻结投影须完整；未知模态只跳过可选自动压缩，不赋予容量证明，强制恢复在select提前拒绝。
    # 函数用途: 容量充足、没有来源或模态计量未知时保留原请求，普通媒体仍交给原选定模型。
    def _automatic_noop(self, frozen: ToolLoopRequestInput, tool_source) -> bool:
        from ..conversation.compact import _compact_request_input_ceiling

        projection = project_tool_loop_request(frozen)
        if projection.status != "ready":
            raise ConversationCompactError("完整请求投影未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        if not compact_request_source_supported(frozen, media_policy=configured_media_policy(self.agent)):
            return True
        tokens, _ = projected_model_context_components(projection)
        if tokens < _compact_request_input_ceiling(self.agent, self.source.policy):
            return True
        return not self.source.messages and tool_source is None

    # LLM: 同次归档和原生IR来源复用原active-turn摘要与CAS；projector只替换已证明完整的冻结来源。
    # 函数用途: 没有已结束历史时，以完整请求计量压缩工具往返和归档交接，未知/过大不提交。
    def _compact_active_source(self, params, frozen, interrupted, tool_source, handoff_max_chars):
        from ..conversation.active_turn_compact import (
            ActiveTurnArchiveCompactRequest,
            compact_carried_active_turn_archive,
        )

        before_projection = self.project_candidate(params, frozen, ConversationCompactView(
            self.source.thread.thread_id, self.source.thread.compact_generation,
            self.source.compact_context.view.summary, (), self.source.compact_context.view.operation_evidence,
            {}, self.source.policy.trigger_tokens, False,
        )).projection
        if before_projection.status != "ready":
            raise ConversationCompactError("恢复输入未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        before, _ = projected_model_context_components(before_projection)

        # LLM: 本回调只在原摘要后、写checkpoint前执行；预计代次来自真实binding，不使用原旧thread猜新代次。
        # 函数用途: 把活动摘要和保留区交给宿主纯投影，交回真实完整计量及同次材料。
        def project(summary, retained, generation):
            material = self.project_active_candidate(params, frozen, summary, retained, generation)
            view = ConversationCompactView(
                self.source.thread.thread_id, generation, summary, (), {}, {},
                self.source.policy.trigger_tokens, True, retained_tool_records=retained,
                retained_ir_history=tool_source.retained_ir_history if tool_source is not None else None,
            )
            material = _project_mixed_recovery_material(material, view, handoff_max_chars)
            expected = replace(self.source.compact_context, view=replace(
                self.source.compact_context.view, summary=summary, generation=generation,
            ))
            if material.projection.status != "ready" or material.params.compact_context != expected:
                raise ConversationCompactError("活动恢复候选未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
            tokens, _ = projected_model_context_components(material.projection)
            return ConversationCompactProjection(tokens, material)

        return compact_carried_active_turn_archive(
            self.agent, self.agent.conversation_store, self.source.thread, list(params.archive_tool_calls),
            ActiveTurnArchiveCompactRequest(
                task_attributes=params.task_attributes, request_id=params.request_id, attempt_id=params.attempt_id,
                task_prompt=params.user_prompt, progress_callback=self.progress_callback, interrupt_check=interrupted,
                compact_context=self.source.compact_context, request_projector=project,
                projected_tokens_before=before, provider_surface=_summary_surface(frozen), tool_source=tool_source,
            ),
        )


# LLM: 摘要继续沿原 provider surface，但只消费此次已准备的 system/schema/展示；不得重跑 Registry、推荐或 PromptBuilder。
# 函数用途: 从完整恢复输入派生摘要缓存面，保留原推荐分段，不把摘要面误当作业务容量证明。
def _summary_surface(prepared: ToolLoopRequestInput) -> ConversationCompactProviderSurface:
    layout = prompt_cache_layout(render_prepared_prompt(prepared.prompt_input))
    return ConversationCompactProviderSurface(
        layout.stable_prefix, prepared.native_tools, prepared.system_instruction,
        tuple(section for section in layout.volatile_sections if section[0] == "prompt.tool_recommendations"),
    )


# LLM: 注入索引由宿主提供；历史种子接管摘要时移除旧applied_compact IR，参数与冻结输入同时更新，不依赖工具分区。
# 合同：候选参数与原参数共享同一个 tool_context 列表（dataclasses.replace 只替换下列字段），提交后写入任一份的
# 插话或修复提示对另一份可见；改为复制前须先迁移 test_compact_recovery_release 里的合同测试。
# 函数用途: 将一个摘要候选投影成同次请求的参数和冻结输入，供各宿主统一计量后发送。
def replace_recovery_history(params, frozen, *, history_seed, injection: str, injection_index: int, compact_context):
    from ..backends.tool_ir import CompactionSummary

    injections, fragments = list(params.runtime_injections), list(frozen.prompt_input.injection_fragments)
    if injection_index < 0 or injection_index >= len(injections) or injection_index >= len(fragments):
        raise ConversationCompactError("恢复注入位置未知", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
    injections[injection_index] = fragments[injection_index] = injection
    history = tuple(item for item in frozen.tool_ir_history if not (
        history_seed is not None and isinstance(item, CompactionSummary) and item.source == "applied_compact"
    ))
    candidate_params = replace(params, conversation_history_seed=history_seed, compact_context=compact_context,
                               runtime_injections=injections, live_archive_state=deepcopy(params.live_archive_state),
                               tool_ir_history=deepcopy(list(history)))
    candidate_params = replace(candidate_params, provider_history_messages=_native_provider_history_messages(candidate_params))
    prepared = replace(frozen, prompt_input=replace(frozen.prompt_input, injection_fragments=tuple(fragments)),
                       provider_history_messages=tuple(candidate_params.provider_history_messages),
                       conversation_state=conversation_runtime_state_section(candidate_params), tool_ir_history=history)
    return candidate_params, prepared


# LLM: 候选仅替换摘要文本和证据，原base/coverage直到CAS后才能推进；不读Store或把未提交候选当权威。
# 函数用途: 给各宿主的纯候选投影统一更新请求内摘要载体。
def project_recovery_compact_context(context, view):
    if context is None:
        return None
    if context.thread_id != view.thread_id:
        raise ConversationCompactError("恢复摘要线程不一致", code="COMPACT_SOURCE_CHANGED")
    if not view.is_candidate:
        return context
    return replace(context, view=replace(context.view, summary=view.summary,
        operation_evidence=deepcopy(view.operation_evidence), generation=view.compact_generation))


# LLM: 只读取获胜CAS返回的head，不load更新后的别人的thread；补齐真实checkpoint与coverage，不改变已验payload。
# 函数用途: 提交成功后把候选参数和宿主状态中的临时base换成实际生效视图。
def _committed_recovery_material(agent, source, result, material):
    from ..conversation.compact_summary_view import resolve_compact_summary_view

    context = source.compact_context
    if context is None:
        return material
    view = resolve_compact_summary_view(agent, result.thread, context.scope)
    candidate = getattr(material.params, "compact_context", None)
    if (candidate is None or candidate.scope != context.scope or candidate.thread_id != context.thread_id
            or candidate.view.summary != view.summary or candidate.view.operation_evidence != view.operation_evidence
            or view.checkpoint_id != result.thread.compact_checkpoint_id):
        raise ConversationCompactError("提交后摘要与恢复请求不一致", code="COMPACT_REQUEST_PROJECTION_CHANGED")
    applied = replace(context, view=view)
    params = replace(material.params, compact_context=applied)
    host_state = replace(material.host_state, compact_context=applied)
    return replace(material, params=params, host_state=host_state)


# LLM: 只判断本请求可见的归档记录或冻结原生工具往返是否存在，不证明身份、不读文件；用于区分"无来源"与"来源身份不可证明"。
# 函数用途: 强制恢复找不到可压工具来源时，判断是确实没有工具记录，还是记录存在但身份无法证明。
def _recovery_tool_records_present(agent, source, params, frozen) -> bool:
    from ..backends.tool_ir import AssistantTurn, ToolResult
    from ..conversation.active_turn_compact import model_visible_active_turn_tool_calls

    visible = model_visible_active_turn_tool_calls(
        agent, params.task_attributes, list(params.archive_tool_calls), compact_context=source.compact_context,
    )
    return bool(visible) or any(isinstance(item, (AssistantTurn, ToolResult)) for item in frozen.tool_ir_history)


# LLM: 从本请求已应用view可见归档及冻结IR选择来源；分区不读文件、不生成摘要，不与别的scope覆盖混合。
# 函数用途: 给活动或混合恢复提供一次工具分区，未知身份和未配对原生记录保持在保留区。
def _recovery_tool_source(agent, source, params, frozen):
    from ..conversation.active_turn_compact import model_visible_active_turn_tool_calls
    from .compact_tool_partition import partition_recovery_tool_source

    if source.compact_context is None:
        raise ConversationCompactError("联合恢复缺少应用视图", code="COMPACT_SOURCE_CHANGED")
    visible = model_visible_active_turn_tool_calls(
        agent, params.task_attributes, list(params.archive_tool_calls), compact_context=source.compact_context,
    )
    return partition_recovery_tool_source(
        visible, frozen.tool_ir_history, covered_tool_refs=source.compact_context.view.source_tool_refs,
    )



# LLM: 宿主先投影候选历史；显式保留工具集再替换同次冻结交接，计量和发送只使用完成两种替换后的同一材料。
# 函数用途: 统一三宿主的联合来源候选，保留控制/媒体/指导，未知IR不能降级成只压聊天。
def _project_mixed_recovery_material(material, view, max_chars):
    if view.retained_tool_records is None:
        return material
    from .compact_active_projection import replace_recovery_active_tools

    params, prepared = replace_recovery_active_tools(
        material.params, material.request_input, compact_context=material.params.compact_context,
        retained_records=view.retained_tool_records, max_chars=max_chars,
        retained_ir_history=view.retained_ir_history,
    )
    return replace(material, params=params, request_input=prepared, projection=project_tool_loop_request(prepared))
