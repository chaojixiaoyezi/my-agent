# LLM: Gateway apply 只消费同请求的一次建议；完整输入验证在锁外，目录→T→thread CAS 在发送前，未知保留原模型。
# 模块用途: 复用公共完整请求捕获、原依赖和线程版本自动采用模型，不创建 renderer、模型注册表或恢复代理。
from __future__ import annotations

import json
import logging
import time
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace

from .agent_core.tool_request_capture import capture_tool_loop_request
from .agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    project_tool_loop_request,
    text_request_capacity_known,
)
from .backends.base import ProviderRequestOptions
from .backends.bounded_call import call_with_deadline
from .backends.request_content import text_content_supported
from .backends.request_scope import foreground_model_scope, provider_request_budget
from .common.cancellation import ToolCancelled
from .concurrency.interrupt import is_interrupted
from .conversation import decision_service
from .gateway_parts.request_binding import GatewayActiveTurnTransition, gateway_runtime_authority
from .memory_archive import estimate_tokens
from .model_guidance import provider_system_instruction
from .model_request_selection import ModelRequestSelectionRejected
from .prompting_parts.builder import PromptBuilder, render_prepared_prompt
from .settings.model_profiles import model_profile_generation, selected_model_config
from .settings.model_scope import model_dependencies_scope, prepare_model_dependencies

MODEL_ADOPTION_KEY = "gateway_model_selection.v1"


# LLM: apply 候选的公开说明必须对应同代原配置；旧目录只在已开启时自动初始化，任何未知或变化的候选不产生采用资格。
# 函数用途: 在原目录内冻结可供本次建议使用的模型代次，不复制凭据到候选或持久请求。
def freeze_selection_candidates(agent: object, candidates: dict, deadline: float) -> tuple[dict, dict]:
    rows, generations = {}, {}
    for profile_id in candidates:
        if time.monotonic() >= deadline:
            raise TimeoutError("候选准备已到期。")
        generation = model_profile_generation(agent, profile_id, initialize=True)
        if generation is None:
            continue
        config = selected_model_config(agent, profile_id=profile_id)
        if model_profile_generation(agent, profile_id) != generation:
            continue
        generations[profile_id] = generation
        rows[profile_id] = {**candidates[profile_id], "model_name": config.model_name,
                           "model_backend": config.model_backend,
                           "declared_context_window_tokens": config.model_context_window_tokens}
    return rows, generations


# LLM: 原 schema 消费口径来自实际 backend builder，thinking_disabled 仅在实际 tools 参数存在时生效；不生成替代载荷。
# 函数用途: 构造与真实 generate 相同的纯 payload，用于准备与最后发送边界逐字核对。
def _payload(backend: object, prompt: str, tools: object, choice: object, messages: object, system: str) -> dict:
    projector = getattr(backend, "project_generate_payload", None)
    if not callable(projector):
        raise ValueError("provider_request_surface_unknown")
    value = projector(prompt, tools=tools, tool_choice=choice if tools is not None else None, messages=messages,
                      request_options=ProviderRequestOptions(system_instruction=system,
                                                             thinking_disabled=tools is not None and choice.mode != "auto"))
    if not isinstance(value, dict):
        raise ValueError("provider_request_surface_unknown")
    return value


# LLM: 原估算与完整 UTF-8 字节工程预算取大，再加实际 output cap；不是供应商精确上界，原 overflow/Compact 仍负责实际拒绝。
# 函数用途: 用大幅余量保守筛选 text/tool 请求的 250K/1M 选择，未知模态在此前保留原模型。
def _capacity(config: object, payload: dict) -> dict:
    window, cap = config.model_context_window_tokens, payload.get("max_tokens")
    if not config.model_context_window_explicit or type(window) is not int or window <= 0 or type(cap) is not int or cap <= 0:
        raise ValueError("request_capacity_unknown")
    count = estimate_tokens(payload)
    # UTF-8 每字节按一个 token 预留；额外协议余量覆盖常见模板/消息/工具条目，不宣称未知 provider 的数学证明。
    wire_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    margin = 4096 + 256 * len(payload.get("messages") or ()) + 1024 * len(payload.get("tools") or ())
    bound = max(count, wire_bytes + margin)
    if bound + cap >= window:
        raise ValueError("request_capacity_exceeded")
    return {"input_tokens_estimate": count, "conservative_input_bound": bound, "estimated": True,
            "budget_basis": "utf8_with_protocol_margin", "output_cap_tokens": cap, "context_window_tokens": window}


# LLM: 此对象只由已领取准确车道的内部请求建立，依赖绑定退出由 caller 执行；worker 只验证和提交，不清理 ContextVar token。
# 类用途: 保持候选在原完整请求、发送 CAS 和后续 Compact/工具轮之间的一致性。
@dataclass
class GatewayModelAdoption:
    observation: object
    thread: object
    stage: object
    outcome: object
    generation: object
    considered: bool = False
    submitted: bool = False
    commit_uncertain: bool = False
    prompt_input: object | None = field(default=None, repr=False)
    candidate: object | None = field(default=None, repr=False)
    candidate_params: object | None = field(default=None, repr=False)
    expected_payload: dict | None = field(default=None, repr=False)
    validation: dict = field(default_factory=dict)
    stack: ExitStack = field(default_factory=ExitStack, repr=False)

    # LLM: 匹配原主请求而非任意同线程任务；context_scope/task_local 不得借主 Gateway 建议。
    # 函数用途: 限制可选采用只作用于自己的实际运行参数。
    def matches(self, agent: object, params: object) -> bool:
        attrs = getattr(params, "task_attributes", {}) or {}
        return (agent is self.observation.context.agent and params.request_id == self.observation.context.request_id
                and attrs.get("conversation_thread_id") == self.thread.thread_id
                and params.context_scope != "task_local")

    # LLM: 不支持原 prepare/render 分离的自定义 builder 仍沿原路径，保持 unknown；已检查一次后不再 gather 或更换模型。
    # 函数用途: 在原渲染时点捕获完整冻结提示，实际字符串仍由原 renderer 唯一生成。
    def render(self, agent: object, params: object, request: object) -> str | None:
        if self.considered or not self.matches(agent, params):
            return None
        builder = agent.prompts
        if not isinstance(builder, PromptBuilder) or type(builder).build is not PromptBuilder.build:
            return None
        self.prompt_input = builder.prepare_render_input(request)
        return render_prepared_prompt(self.prompt_input)

    # LLM: 整个请求只准备一次；晚返回 worker 无提交权限。绑定只在 caller 完成，结束时退出，不覆盖 Agent 全局字段。
    # 函数用途: 用原有界调用验证候选并临时绑定依赖；任何普通失败继续原模型，取消原样传播。
    def select(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        if self.considered or not self.matches(agent, params):
            return params, prompt
        self.considered = True
        try:
            if (self.prompt_input is None or render_prepared_prompt(self.prompt_input) != prompt
                    or self.thread.model_profile_id != self.observation.captured.profile_id):
                raise ValueError("request_facts_unknown")
            if model_profile_generation(agent, self.generation.profile_id) != self.generation:
                raise ValueError("model_catalog_changed")
            config = selected_model_config(agent, profile_id=self.generation.profile_id)
            if model_profile_generation(agent, self.generation.profile_id) != self.generation:
                raise ValueError("model_catalog_changed")
            dependencies = prepare_model_dependencies(agent, config)
            if dependencies is None:
                raise ValueError("model_dependencies_unknown")
            prepared = capture_tool_loop_request(agent, params, self.prompt_input)
            if not text_request_capacity_known(prepared, allow_reasoning=False):
                raise ValueError("history_modality_unknown")
            frozen_params = replace(params, tool_ir_history=deepcopy(params.tool_ir_history),
                                    provider_history_messages=deepcopy(params.provider_history_messages),
                                    tool_context=deepcopy(params.tool_context), live_archive_state=deepcopy(params.live_archive_state))
            candidate_params, payload, validation = call_with_deadline(
                lambda: self._prepare(agent, frozen_params, prepared, dependencies), deadline=self.outcome.deadline,
                resource_key=("gateway_model_probe", self.stage.owner_ref, self.generation.profile_id,
                              self.generation.catalog_generation, self.generation.shared_generation), optional=True,
            )
            self.candidate, self.candidate_params = dependencies, candidate_params
            self.expected_payload, self.validation = payload, validation
            self.stack.enter_context(model_dependencies_scope(agent, dependencies, thread_id=self.thread.thread_id, active=True))
            self.stack.enter_context(foreground_model_scope(agent.backend))
            return candidate_params, prompt
        except (InterruptedError, ToolCancelled):
            raise
        except Exception:
            self.stack.close()
            self.candidate = None
            self._record_retained("candidate_validation_unavailable")
            return params, prompt

    # LLM: provider 探针沿原 bounded budget；原 stage 身份冻结于车道，不改绑当前主 run；仅返回材料，无权采用或迟到提交。
    # 函数用途: 在独立候选依赖中核对工具、完整载荷与原压缩预检，不污染原参数的可变 IR/归档状态。
    def _prepare(self, agent: object, params: object, prepared: ToolLoopRequestInput, dependencies: object):
        from .agent_core.model.context_pressure import preflight_context_pressure_response
        from .agent_core.native_tool_protocol import select_tool_protocol
        from .agent_core.tool_model_generation import ModelGenerateParams

        if not decision_service.decision_outcome_is_current(agent, self.observation.params, self.stage, self.outcome):
            raise ValueError("decision_changed")
        with model_dependencies_scope(agent, dependencies), provider_request_budget(self.outcome.deadline - time.monotonic()):
            protocol = select_tool_protocol(agent, run_id=params.run_id)
            candidate_params = replace(params, tool_protocol_snapshot=protocol)
            candidate_input = replace(prepared, tool_protocol_snapshot=protocol,
                                      system_instruction=provider_system_instruction(agent.backend))
            if not text_request_capacity_known(candidate_input, allow_reasoning=False):
                raise ValueError("history_modality_unknown")
            projected = project_tool_loop_request(candidate_input)
            if projected.status != "ready" or any(not text_content_supported(row.get("content"), allow_reasoning=False) for row in projected.messages or ()):
                raise ValueError("history_modality_or_projection_unknown")
            # 原始非空工具面即使被 choice.none 隐藏，也决定真实发送包装是否关闭 thinking。
            payload = _payload(agent.backend, projected.provider_prompt, list(candidate_input.native_tools) or None, projected.tool_choice,
                               projected.messages, projected.system_instruction)
            validation = _capacity(dependencies.config, payload)
            if preflight_context_pressure_response(ModelGenerateParams(agent, candidate_params, projected.prompt, params.tool_rounds)):
                raise ValueError("original_context_preflight_rejected")
            return candidate_params, payload, {**validation, "provider_tool_support": protocol.capability.evidence}

    # LLM: 只在精确候选首次发送前核对真实最终载荷；提交以后不得把 HTTP 错误当成局部拒绝，也不重新检查后续工具轮模型。
    # 函数用途: 最后复核候选并原子提交模型与发送意图，失败通知 caller 沿原模型一次执行。
    def before_send(self, backend: object, prompt: str, state: object) -> None:
        if self.submitted or not self.matches(state.agent, state.params):
            return
        if not self.considered:
            self.considered = True
            self._record_retained("first_request_not_selected")
        if self.candidate is None:
            return
        try:
            if state.params is not self.candidate_params or backend is not state.agent.backend:
                raise ValueError("request_identity_changed")
            payload = _payload(backend, prompt, state.tools, state.tool_choice, state.messages, state.system_instruction)
            if payload != self.expected_payload:
                raise ValueError("provider_payload_changed")
            _capacity(self.candidate.config, payload)
            if not decision_service.decision_outcome_is_current(state.agent, self.observation.params, self.stage, self.outcome):
                raise ValueError("decision_changed")
            if not self._commit(state):
                raise ValueError("selection_changed")
        except (InterruptedError, ToolCancelled):
            raise
        except Exception as exc:
            if self.submitted or self.commit_uncertain:
                self._record_commit_failure()
                raise
            raise ModelRequestSelectionRejected("gateway_candidate_rejected_before_provider") from exc
        # 原线程 CAS 已确定采用，之后回执故障不能跨模型重发。
        try:
            self.observation.writer.record_adoption({"adopted": True, "status": "send_intent_uncertain",
                                                    "profile_id": self.generation.profile_id, "validation": self.validation,
                                                    "adoption_eligibility": "validated_estimate"})
        except (InterruptedError, ToolCancelled):
            raise
        except Exception:
            logging.getLogger(__name__).warning("模型采用回执未保存，原线程发送意图仍有效", exc_info=False)

    # LLM: model catalog→active-turn T→thread 原锁序，无网络；原目录/设置/Compact/显式选择版本须同时有效。
    # 函数用途: 在唯一线程存储保存一次自动选择及不确定发送意图，保持最近显式事件版本。
    def _commit(self, state: object) -> bool:
        from .settings.model_profiles import model_profile_generation_guard

        context = self.observation.context
        with model_profile_generation_guard(state.agent, self.generation, blocking=False) as current:
            if not current:
                return False
            transition = GatewayActiveTurnTransition(context.request_path, context.request_id, context.request["execution_attempt_id"])
            transition("submit", lambda: self._commit_active_turn(state))
        return self.submitted

    # LLM: 在 T 内重读原队列，不凭进程中旧 runtime_authority 或临时 marker 采用；claim 读取不创建或续租。
    # 函数用途: 确认准确请求仍拥有原身份和车道，再进入线程 CAS。
    def _commit_active_turn(self, state: object) -> None:
        from .gateway_parts.io import read_json_file
        from .gateway_parts.request_binding import MODEL_OBSERVATION_KEY

        context = self.observation.context
        current = read_json_file(context.request_path)
        authority = gateway_runtime_authority(current, context.request_id)
        marker = current.get(MODEL_OBSERVATION_KEY, {})
        if (not authority or any(authority.get(key) != getattr(state.params, key, None) for key in ("run_id", "task_id", "attempt_id"))
                or marker.get("operation_id") != self.stage.operation_id or marker.get("status") != "observed"
                or marker.get("claim_id") != self.observation.claim["claim_id"]):
            return
        claim = state.agent.conversation_store.claims.load(self.thread.thread_id)
        if (claim.get("claim_id") != self.observation.claim["claim_id"] or claim.get("status") != "running"
                or claim.get("expires_at", 0) <= time.time()):
            return
        threads = state.agent.conversation_store.threads
        self.commit_uncertain = True
        try:
            saved = threads.update_atomic(self.thread.thread_id, lambda latest: self._adopt(latest, state))
        except BaseException:
            self._resolve_write_failure(state)
            raise
        self.submitted = self._owns_intent(saved, state.call_id)
        self.commit_uncertain = False

    # LLM: 对更新异常不能猜原子 replace 是否发生；只有原选择版本不变且无本次意图才证明未提交，读不回保留 UNKNOWN。
    # 函数用途: 区分可安全回退的磁盘失败与已经提交或结果不确定的失败，不发送候选或重复原模型。
    def _resolve_write_failure(self, state: object) -> None:
        try:
            saved = state.agent.conversation_store.threads.require(self.thread.thread_id)
        except Exception:
            return
        self.submitted = self._owns_intent(saved, state.call_id)
        unchanged = (saved.model_profile_id, saved.model_selection_revision) == (self.thread.model_profile_id, self.thread.model_selection_revision)
        self.commit_uncertain = self.submitted or not unchanged

    # LLM: 读回只承认本请求/操作/物理调用的原线程发送意图，显示投影或相同模型编号不证明提交。
    # 函数用途: 核对原 thread CAS 返回或故障读回是否确认此次采用。
    def _owns_intent(self, thread: object, call_id: str) -> bool:
        record = thread.metadata.get(MODEL_ADOPTION_KEY, {})
        return (record.get("schema") == MODEL_ADOPTION_KEY and record.get("operation_id") == self.stage.operation_id
                and record.get("request_id") == self.observation.context.request_id and record.get("provider_call_id") == call_id
                and record.get("status") == "send_intent_uncertain" and thread.model_profile_id == self.generation.profile_id)

    # LLM: updater 只能修改本线程选择和一条有界元数据；每次覆盖旧请求记录，发送意图不推断 HTTP 收到，也不授予重新选择资格。
    # 函数用途: 比较 source/revision 与真实运行绑定，提交采用时保持历史、权限和其它元数据。
    def _adopt(self, latest: object, state: object):
        from .settings.decision_settings_projection import decision_settings_projection
        from .settings.model_profiles import model_profiles_path, read_model_profiles
        from .settings.shared_model_catalog import shared_profile_key
        from .user_space.approval_mode import permission_config

        agent, context = state.agent, self.observation.context
        if is_interrupted() or bool(getattr(getattr(state.params, "cancellation_token", None), "cancelled", False)):
            raise InterruptedError("当前模型请求已停止。")
        authority = gateway_runtime_authority(context.request, context.request_id)
        if (not authority or any(authority.get(key) != getattr(state.params, key, None) for key in ("run_id", "task_id", "attempt_id"))
                or time.monotonic() >= self.outcome.deadline
                or (latest.model_profile_id, latest.model_selection_revision, latest.compact_generation) !=
                   (self.thread.model_profile_id, self.thread.model_selection_revision, self.thread.compact_generation)):
            return latest
        settings = decision_settings_projection(agent, read_model_profiles(model_profiles_path(agent.home_paths)), latest)
        point = settings["effective"]["points"]["model_selection"]
        if (point["effective_mode"] != "apply" or decision_service._policy_revision(settings) != self.outcome.response.binding.policy_revision
                or shared_profile_key(point["profile_id"]) and not self.generation.shared_generation):
            return latest
        config = selected_model_config(agent, profile_id=self.generation.profile_id)
        if permission_config(config, agent.home_paths) != self.candidate.config:
            return latest
        record = {"schema": MODEL_ADOPTION_KEY, "request_id": context.request_id, "operation_id": self.stage.operation_id,
                  "execution_attempt_id": context.request["execution_attempt_id"], "run_id": state.params.run_id,
                  "attempt_id": state.params.attempt_id, "profile_id": self.generation.profile_id,
                  "source_selection_revision": latest.model_selection_revision,
                  "model_generation": self.generation.to_dict(), "status": "send_intent_uncertain",
                  "provider_call_id": state.call_id, "validation": self.validation}
        return replace(latest, model_profile_id=self.generation.profile_id,
                       model_selection_revision=latest.model_selection_revision + 1, model_selection_source="automatic",
                       provider_context_observation={}, model_context_usage={},
                       metadata={**latest.metadata, MODEL_ADOPTION_KEY: record})

    # LLM: 仅已准备但尚未提交的本请求可回退；退出候选绑定发生在原 caller，随后 typed 拒绝不再触发第二次选择。
    # 函数用途: 在发送前明确拒绝后恢复原模型及参数，保留原账本中的零 HTTP 尝试。
    def reject(self, agent: object, params: object) -> None:
        if self.submitted or self.commit_uncertain or params is not self.candidate_params or not self.matches(agent, params):
            raise RuntimeError("不能回退已提交或其它请求的模型。")
        self.stack.close()
        self.candidate = None
        self._record_retained("candidate_rejected_before_provider")

    # LLM: 原线程写入已发生或无法证伪时，原请求只展示确认程度；此回执失败不能再转成未提交或允许第二次选择。
    # 函数用途: 为失败收口保留采用不确定性，不谎称已有 HTTP 或原模型可以安全重发。
    def _record_commit_failure(self) -> None:
        try:
            self.observation.writer.record_adoption({
                "status": "send_intent_uncertain" if self.submitted else "commit_unknown",
                "adopted": True if self.submitted else None,
                "reason": "thread_commit_confirmed_io_error" if self.submitted else "thread_commit_unconfirmed",
            })
        except (InterruptedError, ToolCancelled):
            raise
        except Exception:
            logging.getLogger(__name__).warning("模型选择提交结果未知，禁止跨模型重发", exc_info=False)

    # LLM: 失败注释仅是原请求投影，不更改线程选择或重置 once marker；持久失败不影响原请求。
    # 函数用途: 保存安全保留原因，不输出原 prompt、凭据或供应商异常正文。
    def _record_retained(self, reason: str) -> None:
        try:
            self.observation.writer.record_adoption({"status": "retained", "reason": reason, "adopted": False,
                                                     "adoption_eligibility": "retained"})
        except (InterruptedError, ToolCancelled):
            raise
        except Exception:
            logging.getLogger(__name__).warning("模型保留回执未保存，继续原请求", exc_info=False)
