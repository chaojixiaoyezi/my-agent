# LLM: 新建标记只能被原真实 attempt 领取一次；候选探针在锁外，原目录/创建/线程锁内 CAS 后才采用，旧 pending/空账本不是首次证明。
# 模块用途: 自动验证子代理模型建议并冻结首个业务请求；未知保留原模型，发送栅栏先于 I/O，用户不用逐个确认。

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from ...concurrency.interrupt import is_interrupted
from ...conversation.agent_thread_store import SUBAGENT_FIRST_REQUEST_KEY
from ...prompting_parts.builder import PromptBuilder, PromptRenderInput, render_prepared_prompt
from ...settings.thread_model_selection import SUBAGENT_MODEL_ADVICE_KEY
from ...subagents.runner_control import runner_attempt_cancelled
from ..tool_request_projection import ToolLoopRequestInput

_PREPARATION: ContextVar[SubagentFirstRequestPreparation | None] = ContextVar("subagent_first_request_preparation", default=None)


# LLM: 仅真实 runner 范围内捕获首次请求的原生产输入；载体不是持久资格，也不因空历史/空账本自动认定首次。
# 类用途: 在精确 attempt 内传递宿主准备事实，跨模型工作线程复制上下文时保持同一对象，退出即失效。
@dataclass
class SubagentFirstRequestPreparation:
    agent: object = field(repr=False)
    run_id: str
    attempt_id: str
    prompt_input: PromptRenderInput | None = field(default=None, repr=False)
    request_input: ToolLoopRequestInput | None = field(default=None, repr=False)
    prompt_request: object | None = field(default=None, repr=False)
    selection_reason: str = "request_validation_unavailable"
    selection_checked: bool = False
    submitted: bool = False


# LLM: 原 attempt_executor 后只原子领取新线程未领取标记；准备中崩溃不复活资格，旧 thread/pending 不回填，真实模型尚未变更。
# 函数用途: 把一次首请求验证绑定原执行轮；领取写 canonical thread，退出清理临时准备，不发网络。
@contextmanager
def subagent_first_request_scope(agent: object, run_id: str, attempt_id: str):
    manager = getattr(agent, "subagents", None)
    store = getattr(manager, "conversation_store", None)
    preparation = None
    if store is not None:
        task = manager.load(run_id)
        thread = store.threads.load(str(task.agent_thread_id or ""))
        marker, advice = _first_request_records(thread)
        if (marker is not None and advice is not None and marker.get("schema") == SUBAGENT_FIRST_REQUEST_KEY
                and marker.get("status") == "unsubmitted"
                and marker.get("child_run_id") == run_id and marker.get("child_thread_id") == task.agent_thread_id
                and marker.get("operation_id") == advice.get("operation_id")):
            with manager.creation_guard():
                _require_current_attempt(manager, SimpleNamespace(attempt_id=attempt_id), run_id)
                won = False
                # LLM: 从最新标记执行一次领取；旧读或另一个启动者不能共用未提交资格。
                # 函数用途: 先持久化准备归属，使崩溃重启自动保留而非重放建议。
                def claim(latest):
                    nonlocal won
                    latest_marker, latest_advice = _first_request_records(latest)
                    if latest_marker != marker or latest_advice != advice:
                        return latest
                    won = True
                    return replace(latest, metadata={**latest.metadata, SUBAGENT_FIRST_REQUEST_KEY: {
                        **marker, "status": "preparing", "attempt_id": attempt_id,
                    }})

                store.threads.update_atomic(task.agent_thread_id, claim)
            if won:
                preparation = SubagentFirstRequestPreparation(agent, run_id, attempt_id)
    token = _PREPARATION.set(preparation)
    try:
        yield preparation
    finally:
        _PREPARATION.reset(token)


# LLM: 进程内准备只给同一 Agent/run/attempt 使用；没有原范围、已发送或身份冲突时始终 unknown，不从 task attrs 重建。
# 函数用途: 为原 prompt 和生成入口找到自己的临时准备对象。
def first_request_preparation(agent: object, params: object) -> SubagentFirstRequestPreparation | None:
    preparation = _PREPARATION.get()
    if (preparation is None or preparation.agent is not agent or preparation.submitted
            or preparation.run_id != getattr(params, "run_id", None)
            or preparation.attempt_id != getattr(params, "attempt_id", None)):
        return None
    return preparation


# LLM: 只有原 PromptBuilder 可分离一次宿主读取与纯渲染；自定义 renderer 继续原 build 并保持输入未知，不重复采集 Goal/文件/Skill。
# 函数用途: 保存实际首请求使用的提示输入，调用原纯渲染器，默认关闭时完全沿用原 build。
def render_first_request_prompt(agent: object, params: object, request: object) -> str:
    preparation = first_request_preparation(agent, params)
    builder = agent.prompts
    if preparation is None or not isinstance(builder, PromptBuilder) or type(builder).build is not PromptBuilder.build:
        return builder.build(request.user_prompt, request.memories, inject=request.inject,
                             prompt_files=request.prompt_files, tools=request.tools,
                             system_prompt_override=request.system_prompt_override, context_scope=request.context_scope,
                             workspace_context_override=request.workspace_context_override)
    prepared = builder.prepare_render_input(request)
    preparation.prompt_input = prepared
    preparation.prompt_request = deepcopy(request)
    return render_prepared_prompt(prepared)


# LLM: 这是有宿主状态读取的准备层，不是纯预览；使用实际 child 参数和原 tools/choice/去重生产方，不填占位历史或借父快照。
# 函数用途: 在原生成入口提交 IR 前冻结完整请求材料；缺原规范历史时保持 unknown，取消原样传播。
def capture_first_request_input(agent: object, params: object, prompt: str) -> None:
    preparation = first_request_preparation(agent, params)
    if preparation is None or preparation.prompt_input is None:
        return
    from ...common.cancellation import ToolCancelled
    from ...conversation.models import ConversationHistorySeed
    from ...model_guidance import provider_system_instruction
    from ..native_tool_protocol import resolve_native_tools
    from ..runtime.conversation_state import conversation_runtime_state_section
    from ..tool_model_generation import _forwarded_guidance_seen, _model_turn_tool_choice

    preparation.request_input = None
    if not isinstance(getattr(params, "conversation_history_seed", None), ConversationHistorySeed):
        return
    try:
        if render_prepared_prompt(preparation.prompt_input) != prompt:
            return
        tools = resolve_native_tools(agent, params)
        preparation.request_input = ToolLoopRequestInput(
            prompt_input=preparation.prompt_input, system_instruction=provider_system_instruction(agent.backend),
            tool_protocol_snapshot=params.tool_protocol_snapshot, native_tools=tuple(tools or ()),
            tool_choice=_model_turn_tool_choice(params, tools), tool_ir_history=tuple(params.tool_ir_history),
            provider_history_messages=tuple(params.provider_history_messages), tool_context=tuple(params.tool_context),
            forwarded_guidance=frozenset(_forwarded_guidance_seen(params)),
            conversation_state=conversation_runtime_state_section(params),
        )
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        preparation.request_input = None


# LLM: task_local 也可用于未注册子代理的普通局部运行；仅已登记的 canonical child 才有首次发送栅栏，不信任 task attrs。
# 函数用途: 在真实子代理模型触网前核对当前执行轮，把首次发送意图与自动保留结果写入同一个原子线程更新。
def mark_subagent_business_request_submitted(agent: object, params: object, *, provider_call_id: str) -> None:
    if getattr(params, "context_scope", "") != "task_local":
        return
    manager = getattr(agent, "subagents", None)
    store = getattr(manager, "conversation_store", None)
    run_id = str(getattr(params, "run_id", "") or "")
    if store is None or not run_id:
        return
    try:
        task = manager.load(run_id)
    except FileNotFoundError:
        # 普通 task_local 运行没有子代理记录；它也没有建议/标记可提交。
        return
    thread_id = str(task.agent_thread_id or "")
    if not thread_id:
        return
    thread = store.threads.require(thread_id)
    marker, advice = _first_request_records(thread)
    if advice is None and (marker is None or marker.get("status") not in {"unsubmitted", "preparing", "reserved"}):
        return
    if store is not getattr(agent, "conversation_store", None):
        raise RuntimeError("子代理首请求不能使用其他会话存储。")
    with manager.creation_guard():
        attempt_id = _require_current_attempt(manager, params, run_id)

        # LLM: 原线程锁内复读资格与建议；提交前错误不发请求，提交后崩溃/重试不恢复首次选择资格。
        # 函数用途: 保存本次首次发送意图，保留并发显式选择和所有其它线程状态。
        def update(latest):
            metadata = dict(latest.metadata)
            current_marker, current_advice = _first_request_records(latest)
            if latest.thread_id != thread_id or metadata.get("agent_run_id") != run_id:
                raise RuntimeError("子代理首请求线程身份冲突。")
            if current_marker is not None and current_marker.get("status") in {"unsubmitted", "preparing", "reserved"}:
                if (current_marker.get("schema") != SUBAGENT_FIRST_REQUEST_KEY
                        or current_marker.get("child_run_id") != run_id
                        or current_marker.get("child_thread_id") != thread_id
                        or not provider_call_id):
                    raise RuntimeError("子代理首请求资格损坏。")
                metadata[SUBAGENT_FIRST_REQUEST_KEY] = {
                    **current_marker, "status": "submitted", "attempt_id": attempt_id,
                    "provider_call_id": provider_call_id,
                }
            if current_advice is not None and current_advice.get("status") == "pending":
                preparation = first_request_preparation(agent, params)
                metadata[SUBAGENT_MODEL_ADVICE_KEY] = {
                    **current_advice, "status": "retained",
                    "reason": (preparation.selection_reason if preparation is not None else
                               "request_validation_unavailable" if current_marker is not None else "first_request_unproven"),
                }
            return replace(latest, metadata=metadata) if metadata != latest.metadata else latest

        store.threads.update_atomic(thread_id, update)
        preparation = first_request_preparation(agent, params)
        if preparation is not None:
            preparation.submitted = True


# LLM: 这里只取原 schema 字段，不从正文、任务属性或模型返回重建资格；无标记的旧线程不获得新资格。
# 函数用途: 读取宿主首请求标记及仍待验证的建议，调用方必须在原子更新里再次读取。
def _first_request_records(thread: object) -> tuple[dict | None, dict | None]:
    metadata = getattr(thread, "metadata", {})
    marker = metadata.get(SUBAGENT_FIRST_REQUEST_KEY)
    advice = metadata.get(SUBAGENT_MODEL_ADVICE_KEY)
    return (marker if isinstance(marker, dict) else None,
            advice if isinstance(advice, dict) and advice.get("status") == "pending" else None)


# LLM: 原创建锁内校验 canonical RUNNING 与精确 attempt，并复用原 DB/control 判据；不手填状态或从线程标记推断 lease。
# 函数用途: 拒绝停止、换轮或无执行凭证的发送；中断按原异常传播，不自动恢复旧任务。
def _require_current_attempt(manager: object, params: object, run_id: str) -> str:
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    token = getattr(params, "cancellation_token", None)
    if is_interrupted() or bool(getattr(token, "cancelled", False)):
        raise InterruptedError("子代理首请求已停止。")
    task = manager.load(run_id)
    if (not attempt_id or task.status != "RUNNING" or task.runner_active_attempt_id != attempt_id
            or runner_attempt_cancelled(manager, run_id, attempt_id)):
        raise InterruptedError("子代理首请求执行轮已失效。")
    return attempt_id


# LLM: 只在原首次完整 prompt 安全点验证一次；准备/探针失败自动 retain，取消传播；只有 CAS 之后才延长同批依赖到原收尾。
# 函数用途: 自动把一条 pending 建议变成经过完整请求验证的当前模型，或继续原模型；不向用户请求逐 child 操作。
def select_first_request_model(agent: object, params: object, prompt: str) -> tuple[object, str]:
    from ...backends.bounded_call import BoundedCallTimeoutError
    from ...common.cancellation import ToolCancelled
    from ...settings.model_scope import (
        activate_model_dependencies,
        model_dependency_lifetime_active,
    )
    from ..native_tool_protocol import ToolProtocolSelectionError

    preparation = first_request_preparation(agent, params)
    if preparation is None or preparation.selection_checked:
        return params, prompt
    preparation.selection_checked = True
    capture_first_request_input(agent, params, prompt)
    if preparation.request_input is None or preparation.prompt_request is None or not model_dependency_lifetime_active(agent):
        return params, prompt
    try:
        candidate = _prepare_candidate(agent, params, preparation)
        if candidate is None:
            return params, prompt
    except (InterruptedError, ToolCancelled):
        raise
    except _CandidateUnavailable as exc:
        preparation.selection_reason = exc.reason
        return params, prompt
    except ToolProtocolSelectionError:
        preparation.selection_reason = "provider_tool_support_unknown"
        return params, prompt
    except (BoundedCallTimeoutError, TimeoutError):
        preparation.selection_reason = "candidate_validation_deadline"
        return params, prompt
    except Exception:
        preparation.selection_reason = "candidate_validation_unavailable"
        return params, prompt
    try:
        committed = _commit_candidate(agent, params, candidate)
    except BlockingIOError:
        committed = False
    if not committed:
        preparation.selection_reason = "selection_changed"
        return params, prompt
    # 已提交后绑定错误必须沿原失败路径传播，不能发一枪与 canonical profile 不同的继承模型。
    activate_model_dependencies(agent, candidate.dependencies, thread_id=candidate.thread.thread_id)
    preparation.prompt_input = candidate.request_input.prompt_input
    preparation.request_input = candidate.request_input
    return candidate.params, render_prepared_prompt(candidate.request_input.prompt_input)


# LLM: 本进程候选载体仅组合原事实，不持有授权；实际使用前须以原目录代次、设置、task 和 thread CAS 全部复核。
# 类用途: 在锁外准备与锁内采用之间传递同一模型依赖、完整输入和原身份，不落盘敏感请求内容。
@dataclass(frozen=True)
class _CandidateRequest:
    thread: object
    advice: dict
    generation: object
    task_fingerprint: str
    dependencies: object = field(repr=False)
    params: object = field(repr=False)
    request_input: ToolLoopRequestInput = field(repr=False)
    validation: dict
    deadline: float


# LLM: 仅传递宿主客观验证原因，不承载原响应/连接/授权；外层将其落为 retained，不能从异常正文决定权限。
# 类用途: 区分完整输入未知与容量超限，让保留结果可以核对。
class _CandidateUnavailable(ValueError):
    # LLM: 原因必须由本模块固定调用点给出，不保存 provider 原文。
    # 函数用途: 保存不包含秘密的客观缺口代码。
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# LLM: 验证顺序固定为版本→原配置→原依赖→候选真实探针→同源 payload；没有版本/完整窗口/出站上限则自动保留，不使用父快照。
# 函数用途: 在原宿主线程准备一个候选，并有界等待锁外工具探针；任何迟到结果都没有提交权。
def _prepare_candidate(agent: object, params: object, preparation: SubagentFirstRequestPreparation) -> _CandidateRequest | None:
    import time

    from ...backends.bounded_call import call_with_deadline
    from ...settings.decision_settings import execute_decision_settings_operation
    from ...settings.model_profiles import model_profile_generation, selected_model_config
    from ...settings.model_provider_schema import ModelProfileGeneration
    from ...settings.model_scope import prepare_model_dependencies
    from ...settings.services.runtime_config_task import project_task_runtime_config_overlay
    from ..orchestration.decision_subagent import SubagentModelInput, _child_fingerprint

    manager = agent.subagents
    task = manager.load(preparation.run_id)
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    marker, advice = _first_request_records(thread)
    if (advice is None or marker is None or marker.get("status") != "preparing"
            or marker.get("attempt_id") != preparation.attempt_id
            or thread.model_selection_source != "inherited" or thread.model_selection_last_explicit_revision):
        preparation.selection_reason = "first_request_unproven"
        return None
    if advice.get("source_model_generation") is None:
        preparation.selection_reason = "model_catalog_generation_unknown"
        return None
    generation = ModelProfileGeneration.from_dict(advice["source_model_generation"])
    if generation.profile_id != advice.get("profile_id") or model_profile_generation(agent, generation.profile_id) != generation:
        preparation.selection_reason = "model_catalog_changed"
        return None
    settings = execute_decision_settings_operation(agent, "read", {}, thread_id=advice["source_thread_id"], blocking=False)
    if not _advice_settings_current(agent, thread, advice, settings):
        preparation.selection_reason = "settings_changed"
        return None
    seconds = settings["effective"]["points"]["subagent_model"]["max_request_seconds"]
    deadline = time.monotonic() + seconds
    config = project_task_runtime_config_overlay(selected_model_config(agent, profile_id=generation.profile_id), task,
                                                  workspace_root=manager.workspace_root)
    dependencies = prepare_model_dependencies(agent, config, inherited=True)
    if dependencies is None:
        return None
    with manager.creation_guard():
        _require_current_attempt(manager, params, preparation.run_id)
    # 原有界 worker 只处理纯依赖和原探针，返回后仍在宿主执行 CAS；超时 worker 不能晚提交。
    candidate_params, request_input, validation = call_with_deadline(
        lambda: _candidate_request_input(agent, params, preparation, dependencies, deadline),
        deadline=deadline, resource_key=("subagent_model_probe", advice["source_owner_ref"], generation.profile_id,
                                         generation.catalog_generation, generation.shared_generation), optional=True,
    )
    return _CandidateRequest(thread, advice, generation, _child_fingerprint(SubagentModelInput({}, task)),
                             dependencies, candidate_params, request_input, validation, deadline)


# LLM: 只读同一 child 的冻结 host 输入；原 provider 探针有请求预算，完整 wire payload 走实际适配器共享 builder，计数复用唯一估算器。
# 函数用途: 在候选依赖中核对 native 工具支持及输入加真实输出上限，返回后续工具轮将直接使用的完整参数。
def _candidate_request_input(agent: object, params: object, preparation: SubagentFirstRequestPreparation,
                             dependencies: object, deadline: float) -> tuple[object, ToolLoopRequestInput, dict]:
    import time

    from ...backends.base import ProviderRequestOptions
    from ...backends.request_scope import provider_request_budget
    from ...memory_archive import estimate_tokens
    from ...model_guidance import provider_system_instruction
    from ...settings.model_scope import model_dependencies_scope
    from ..model.context_pressure import preflight_context_pressure_response
    from ..native_tool_protocol import resolve_native_tools, select_tool_protocol
    from ..tool_model_generation import ModelGenerateParams, _model_turn_tool_choice
    from ..tool_request_projection import project_tool_loop_request

    with model_dependencies_scope(agent, dependencies), provider_request_budget(deadline - time.monotonic()):
        protocol = select_tool_protocol(agent, run_id=params.run_id)
        candidate_params = replace(params, tool_protocol_snapshot=protocol)
        prompt_input = agent.prompts.prepare_render_input(preparation.prompt_request)
        tools = resolve_native_tools(agent, candidate_params)
        request_input = replace(preparation.request_input, prompt_input=prompt_input,
                                tool_protocol_snapshot=protocol, native_tools=tuple(tools or ()),
                                tool_choice=_model_turn_tool_choice(candidate_params, tools),
                                system_instruction=provider_system_instruction(agent.backend))
        projected = project_tool_loop_request(request_input)
        projector = getattr(agent.backend, "project_generate_payload", None)
        if projected.status != "ready" or not callable(projector):
            raise _CandidateUnavailable("provider_request_surface_unknown")
        payload = projector(projected.provider_prompt, tools=projected.tools, tool_choice=projected.tool_choice,
                            messages=projected.messages, request_options=ProviderRequestOptions(
                                system_instruction=projected.system_instruction,
                                thinking_disabled=projected.tool_choice.mode != "auto",
                            ))
        config = dependencies.config
        window = config.model_context_window_tokens
        cap = payload.get("max_tokens") if isinstance(payload, dict) else None
        estimated = estimate_tokens(payload) if isinstance(payload, dict) else None
        if (not config.model_context_window_explicit or type(window) is not int or window <= 0
                or type(cap) is not int or cap <= 0 or estimated is None):
            raise _CandidateUnavailable("request_capacity_unknown")
        if estimated + cap >= window:
            raise _CandidateUnavailable("request_capacity_exceeded")
        if preflight_context_pressure_response(ModelGenerateParams(agent, candidate_params, projected.prompt, params.tool_rounds)):
            raise _CandidateUnavailable("original_context_preflight_rejected")
        return candidate_params, request_input, {
            "input_tokens_estimate": estimated, "output_cap_tokens": cap, "context_window_tokens": window,
            "provider_tool_support": protocol.capability.evidence,
        }


# LLM: 源设置只来自可信 owner 与原父线程；pending 身份、来源修订、当前 apply 和候选范围一起核对，不因语义建议扩大授权。
# 函数用途: 判断创建时建议在当前配置与父子关联下仍有效；关闭、范围变更或来源撤销自动保留。
def _advice_settings_current(agent: object, thread: object, advice: dict, settings: dict) -> bool:
    from ...conversation.decision_policy import decision_owner_ref

    point = settings["effective"]["points"]["subagent_model"]
    allowed = point["candidate_profile_ids"]
    return (advice.get("schema") == SUBAGENT_MODEL_ADVICE_KEY and advice.get("status") == "pending"
            and advice.get("source_owner_ref") == decision_owner_ref(agent)
            and advice.get("source_thread_id") == thread.metadata.get("parent_agent_thread_id")
            and advice.get("child_thread_id") == thread.thread_id
            and advice.get("child_run_id") == thread.metadata.get("agent_run_id")
            and settings["revision"] == {"owner": advice.get("source_owner_revision"), "thread": advice.get("source_thread_revision")}
            and point["effective_mode"] == "apply" and point["connection"]["configured"]
            and (not allowed or advice.get("profile_id") in allowed))


# LLM: 配置→父线程→creation→child 锁序；锁内只读原配置并 CAS，无网络；模型、版本、建议终态和发送预留同次写入唯一 thread。
# 函数用途: 最后复核权限、设置、原 attempt 与 pending，再原子采用；返回 False 时原模型和其它线程字段均保留。
def _commit_candidate(agent: object, params: object, candidate: _CandidateRequest) -> bool:
    import time

    from ...common.json_io import locked_json_path
    from ...settings.decision_settings_projection import decision_settings_projection
    from ...settings.model_profiles import (
        model_profile_generation_guard,
        model_profiles_path,
        read_model_profiles,
        selected_model_config,
    )
    from ...settings.services.runtime_config_task import project_task_runtime_config_overlay
    from ...settings.thread_model_selection import _require_owner
    from ...user_space.approval_mode import permission_config
    from ..orchestration.decision_subagent import SubagentModelInput, _child_fingerprint

    manager, threads = agent.subagents, agent.conversation_store.threads
    source_id = candidate.advice["source_thread_id"]
    if source_id == candidate.thread.thread_id:
        return False
    with model_profile_generation_guard(agent, candidate.generation, blocking=False) as current:
        if not current:
            return False
        with locked_json_path(threads.storage.thread_path(source_id), blocking=False):
            source = threads.load(source_id)
            if source is None:
                return False
            _require_owner(agent, source)
            settings = decision_settings_projection(agent, read_model_profiles(model_profiles_path(agent.home_paths)), source)
            if not _advice_settings_current(agent, candidate.thread, candidate.advice, settings):
                return False
            with manager.creation_guard():
                _require_current_attempt(manager, params, params.run_id)
                task = manager.load(params.run_id)
                if _child_fingerprint(SubagentModelInput({}, task)) != candidate.task_fingerprint:
                    return False
                config = project_task_runtime_config_overlay(selected_model_config(agent, profile_id=candidate.generation.profile_id),
                                                             task, workspace_root=manager.workspace_root)
                if permission_config(config, agent.home_paths, inherited=True) != candidate.dependencies.config:
                    return False
                if time.monotonic() >= candidate.deadline:
                    return False
                return _adopt_thread(agent, candidate, params)


# LLM: 原 child update_atomic 同时提交选择与一次发送意图；显式同值选择也会让 revision/pending 比较失败，不擦除其它状态或修改已落盘 task。
# 函数用途: 保存已验证候选；reserved 不代表网络已成功，真正 provider 边界另记 submitted 与调用编号。
def _adopt_thread(agent: object, candidate: _CandidateRequest, params: object) -> bool:
    import time

    adopted = False

    # LLM: updater 必须使用锁内最新值；旧建议、旧 revision 或首资格已消费都不覆盖当前会话。
    # 函数用途: 以原单调选择版本保存一次自动采用，保持显式事件版本和其余元数据。
    def update(latest):
        nonlocal adopted
        _require_current_attempt(agent.subagents, params, params.run_id)
        marker, advice = _first_request_records(latest)
        if (time.monotonic() >= candidate.deadline or advice != candidate.advice or marker is None
                or marker.get("status") != "preparing" or marker.get("attempt_id") != params.attempt_id
                or marker.get("operation_id") != advice.get("operation_id")
                or (latest.model_profile_id, latest.model_selection_revision) !=
                   (candidate.thread.model_profile_id, candidate.thread.model_selection_revision)):
            return latest
        adopted = True
        return replace(latest, model_profile_id=candidate.generation.profile_id,
                       model_selection_revision=latest.model_selection_revision + 1, model_selection_source="automatic",
                       provider_context_observation={}, model_context_usage={}, metadata={
                           **latest.metadata,
                           SUBAGENT_MODEL_ADVICE_KEY: {**advice, "status": "adopted", "reason": "first_request_validated",
                                                      "validation": candidate.validation},
                           SUBAGENT_FIRST_REQUEST_KEY: {**marker, "status": "reserved"},
                       })

    agent.conversation_store.threads.update_atomic(candidate.thread.thread_id, update)
    return adopted


# LLM: 新工作片/Compact 只解析 canonical child 已生效选择，不读取旧建议或重跑决策；原继承模型保持原作用域，失败沿配置错误传播。
# 函数用途: 让自动采用后的 Goal 续轮、恢复及压缩读取同一有效模型，进入/退出仍只绑定原 ContextVar。
@contextmanager
def canonical_subagent_model_scope(agent: object, run_id: str, *, task_local: bool = True):
    from ...backends.request_scope import foreground_model_scope
    from ...settings.model_profiles import inherited_model_config
    from ...settings.model_scope import model_dependencies_scope, prepare_model_dependencies
    from ...settings.services.runtime_config_task import project_task_runtime_config_overlay

    manager = getattr(agent, "subagents", None)
    task = None
    if task_local and manager is not None and run_id:
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            pass
    store = getattr(manager, "conversation_store", None)
    thread = store.threads.load(task.agent_thread_id) if task is not None and store is not None else None
    if thread is None or thread.model_selection_source not in {"automatic", "explicit"}:
        yield
        return
    config = project_task_runtime_config_overlay(inherited_model_config(agent, task), task, workspace_root=manager.workspace_root)
    dependencies = prepare_model_dependencies(agent, config, inherited=True)
    if dependencies is None:
        raise RuntimeError("子代理选定模型缺少原依赖作用域。")
    with model_dependencies_scope(agent, dependencies, thread_id=thread.thread_id, active=True), foreground_model_scope(dict(dependencies.values)["backend"]):
        yield
