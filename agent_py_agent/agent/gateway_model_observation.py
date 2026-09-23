# LLM: Gateway 只在准确车道后请求一次建议；off 保持排队前原冻结，apply 仅在完整首请求和发送 CAS 验证后采用。
# 模块用途: 绑定默认关闭的模型建议与原请求安全点；延迟Compact沿同一作用域运行，不重新决策或授予恢复权限。
from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from .backends.decision_protocol import decision_json
from .command_catalog import system_slash_command_name
from .common.cancellation import ToolCancelled
from .contracts.idempotency import operation_id
from .conversation import decision_service
from .gateway_parts.request_binding import (
    MODEL_OBSERVATION_KEY,
    GatewayActiveTurnTransition,
    GatewayModelObservationWriter,
    gateway_request_is_active_turn_recovery,
)
from .settings.decision_settings_projection import decision_point_mode_from_read
from .settings.model_profiles import SelectedModelRead, execute_model_profile_operation

_RETAIN_CHOICES = {
    "retain_original": "沿用当前冻结模型。", "need_data": "缺少作出建议的资料。",
    "not_needed": "不需要额外选择模型。", "no_match": "目录没有适合项。", "abstain": "无法可靠判断。",
}


# LLM: 只引用原目录的当前可访问生成模型，字段白名单不含端点、凭据或价格；不探针、不迁移、不保存选择。
# 函数用途: 准备本次观察的公开模型候选，窗口是配置声明，不代表完整输入能容纳。
def _candidates(agent: object, deadline: float) -> dict:
    public = execute_model_profile_operation(agent, "list", {})
    result = {}
    for row in public["profiles"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("模型观察准备已到期")
        if row["id"] == "default" or not row.get("available") or "agentic" not in row.get("available_for", ()):
            continue
        result[row["id"]] = {
            "model_name": row["model_name"], "model_backend": row["model_backend"],
            "declared_context_window_tokens": row["model_context_window_tokens"],
            "capacity_status": "unknown_until_full_request_projection",
            "tool_support": "unknown_until_original_probe",
        }
    return result


# LLM: 原提示和原摘要仅供语义判断；完整历史、system/native schemas、图像及真实输出预留尚未准备，必须明确未知。
# 函数用途: 描述这一新主请求和当前冻结模型，不把不完整输入伪装成可自动切换的容量证明。
def _observation_input(context: object, thread: object, captured: SelectedModelRead, candidates: dict) -> tuple[dict, dict]:
    config = context.agent.config
    state = {
        "prompt": str(context.request.get("prompt") or context.request.get("goal") or "").strip(),
        "conversation_summary": thread.summary,
        "current_model": {"profile_id": captured.profile_id, "model_name": config.model_name,
                          "model_backend": config.model_backend, "context_window_tokens": config.model_context_window_tokens},
        "observed_thread_selection": {"profile_id": thread.model_profile_id, "revision": thread.model_selection_revision},
        "input_completeness": {"full_history": "unknown", "system_and_native_schemas": "unknown",
                               "multimodal_inputs": "unknown", "output_and_reasoning_reserve": "unknown"},
    }
    questions = {"model": {"type": "choice", "instructions": (
        "根据当前任务对已授权目录提出一项模型建议。本次只观察，宿主保持原模型。"
        "候选只含配置声明，不能把模型名或窗口当成能力、完整容量或授权证明；资料不足可选择 need_data。"
    ), "criteria": {**candidates, **_RETAIN_CHOICES}}}
    return state, questions


# LLM: 只接收原 service 已验回执的单题结构，错误不会改称无需选择；输出不是后续自动采用凭据。
# 函数用途: 将本次建议变成安全的编号与状态，原始正文、概率和配置不写入队列记录。
def _observation_result(outcome: decision_service.DecisionOutcome, candidates: dict) -> dict:
    if outcome.status != "success" or outcome.response is None:
        return {"status": outcome.status, "reason": outcome.reason}
    answer = next((item for item in outcome.response.answers if item.question_id == "model"), None)
    if (answer is None or answer.error_code or answer.kind != "choice"
            or not isinstance(answer.value, str) or answer.value not in {*candidates, *_RETAIN_CHOICES}):
        return {"status": "error", "reason": "invalid_answer"}
    return {"status": "observed", "choice": answer.value, "adoption_eligibility": "not_evaluated"}


# LLM: 只由原Gateway创建；观察marker和延迟Compact分别守原权威，Compact不借模型建议资格，也不得触发第二次决策。
# 类用途: 连接一次模型观察、可选采用和已绑定请求的压缩恢复，采用仍经原最终发送门。
@dataclass
class GatewayModelObservation:
    context: object
    captured: SelectedModelRead | None
    claim: object
    called: bool = False
    params: object | None = field(default=None, repr=False)
    writer: object | None = field(default=None, repr=False)
    adoption: object | None = field(default=None, repr=False)
    compact_recovery: object | None = field(default=None, repr=False)

    # LLM: 宿主钩子只在原 Gateway 请求内存作用域存在；依赖退出晚于 Compact/收尾，任何异常均由原 caller 清理。
    # 函数用途: 包住本次执行并向原 core 暴露安全点，关闭时不读取任何额外状态。
    @contextmanager
    def scope(self):
        from .model_request_selection import model_request_selection_scope

        with model_request_selection_scope(self):
            try:
                yield
            finally:
                if self.adoption is not None:
                    self.adoption.stack.close()

    # LLM: 待恢复Compact优先冻结当前请求，否则按原可选采用入口；无候选时None沿原renderer，不干预child。
    # 函数用途: 在原提示准备时点保存恢复输入或首次选模输入，避免为候选重复读取材料。
    def render(self, agent: object, params: object, request: object) -> str | None:
        if self.compact_recovery is not None:
            rendered = self.compact_recovery.render(agent, params, request)
            if rendered is not None:
                return rendered
        return self.adoption.render(agent, params, request) if self.adoption is not None else None

    # LLM: 初次及恢复压缩独占当前完整请求准备时点；辅助回复和其它scope不得消费本请求来源。
    # 函数用途: 在冻结提示前暂缓旧自动压缩路径。
    def owns_compact(self, agent: object, params: object) -> bool:
        return self.compact_recovery is not None and self.compact_recovery.owns_compact(agent, params)

    # LLM: 先提交上下文再绑定候选；只更新本次CAS得到的compact代次，选模revision和profile守门不变。
    # 函数用途: 让选模使用压缩后的冻结提示，并让原模型回退基线保留已提交摘要。
    def prepare_request(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        recovery = self.compact_recovery
        if recovery is None or recovery.consumed:
            return params, prompt
        params, prompt = recovery.prepare_request(agent, params, prompt)
        if self.adoption is not None and recovery.resolved_input is not None:
            self.adoption.prompt_input = recovery.resolved_input.prompt_input
            if recovery.committed:
                self.adoption.thread = replace(
                    self.adoption.thread, compact_generation=recovery.committed_thread.compact_generation,
                )
        return params, prompt

    # LLM: 未提交采用与延迟Compact不得混用；恢复先沿原CAS提交且不重决策，普通首请求仍按原采用入口。
    # 函数用途: 在生成前交回已确认的恢复参数，或完成本次可选模型采用。
    def select(self, agent: object, params: object, prompt: str) -> tuple[object, str]:
        if self.compact_recovery is not None and not self.compact_recovery.consumed:
            if self.adoption is not None and self.adoption.candidate is not None and not self.adoption.submitted:
                from .conversation.compact_guard import ConversationCompactError

                raise ConversationCompactError("未提交的模型选择不能恢复压缩", code="COMPACT_MODEL_SELECTION_PENDING")
            return self.compact_recovery.select(agent, params, prompt)
        return self.adoption.select(agent, params, prompt) if self.adoption is not None else (params, prompt)

    # LLM: 精确最终发送由原 worker 回调；没有本请求采用对象时零 I/O 返回。
    # 函数用途: 对候选实际请求执行最后的版本事务。
    def before_send(self, backend: object, prompt: str, state: object) -> None:
        if self.adoption is not None:
            self.adoption.before_send(backend, prompt, state)

    # LLM: 仅 typed 发送前拒绝能够调用，HTTP 错误和取消不进入此分支。
    # 函数用途: 在原 caller 清理候选绑定，随后只执行一次原模型。
    def reject(self, agent: object, params: object) -> None:
        if self.adoption is None:
            raise RuntimeError("没有待撤销的候选。")
        self.adoption.reject(agent, params)

    # LLM: 仅宿主 fresh thread 触发；off 只读内存，没有设置/候选/marker/账本 I/O。取消原样传播，普通增强错误不阻断原主链。
    # 函数用途: 在历史修复和 Compact 之前执行一次可选观察，排队期间的线程开关改动立即生效。
    def __call__(self, thread: object) -> None:
        if self.called:
            return
        self.called = True
        try:
            if not self._eligible(thread):
                return
            mode = decision_point_mode_from_read(self.context.agent.config, self.captured.decision_settings,
                                                thread, point="model_selection")
            if mode == "off":
                return
            self._observe(thread, mode)
        except (InterruptedError, ToolCancelled):
            raise
        except Exception:
            logging.getLogger(__name__).warning("主会话模型观察不可用，继续原模型", exc_info=False)

    # LLM: 车道、请求与 attempt 都由原 Gateway 生成；旧 recovery、runtime authority、控制命令不产生新观察。
    # 函数用途: 用结构化事实筛选普通新请求，不解析普通中文或从模型名称猜运行类型。
    def _eligible(self, thread: object) -> bool:
        request = self.context.request
        claim = self.claim if isinstance(self.claim, dict) else {}
        return bool(
            self.captured is not None and request.get("kind") == "ask"
            and request.get("execution_attempt_id") and not request.get("runtime_authority")
            and not request.get("system_task") and not request.get("resume_context")
            and not gateway_request_is_active_turn_recovery(request, self.context.request_id)
            and MODEL_OBSERVATION_KEY not in request
            and not system_slash_command_name(str(request.get("prompt") or request.get("goal") or "").strip())
            and claim.get("status") == "running" and claim.get("claim_id")
            and claim.get("thread_id") == thread.thread_id
            and claim.get("task_id") == f"gateway:{self.context.request_id}"
        )

    # LLM: marker 先于候选和网络；同一 stage 绝对期限包含准备，apply 仅保存内存候选，正式采用晚于完整请求检查。
    # 函数用途: 用原账本和可选调用资源请求一项建议；普通失败保存安全状态，停止仍终止原主请求。
    def _observe(self, thread: object, mode: str) -> None:
        context = self.context
        op = operation_id("gateway_model_observation", {"request_id": context.request_id, "thread_id": thread.thread_id})
        writer = GatewayModelObservationWriter(context, thread.thread_id, self.claim["claim_id"], op)
        if not writer.reserve():
            return
        self.writer = writer
        self.params = SimpleNamespace(request_id=context.request_id, run_id="", task_id="", source="gateway",
                                      task_attributes={"conversation_thread_id": thread.thread_id})
        facts = {"requested_mode": mode, "frozen_profile_id": self.captured.profile_id,
                 "observed_thread_profile_id": thread.model_profile_id,
                 "observed_selection_revision": thread.model_selection_revision,
                 "observed_selection_source": thread.model_selection_source}
        result = {"status": "error", "reason": "enhancement_failed"}
        try:
            result = self._decide(thread, op, mode)
        except (InterruptedError, ToolCancelled):
            result = {"status": "cancelled", "reason": "user_cancelled"}
            raise
        finally:
            try:
                writer.finish({**facts, **result})
            except (InterruptedError, ToolCancelled):
                raise
            except Exception:
                logging.getLogger(__name__).warning("模型观察回执未落盘，保留原一次性标记", exc_info=False)

    # LLM: 原 service 复核设置/身份/连接；observe 仅记录，apply 也只创建临时候选，不能跳过首请求/发送检查。
    # 函数用途: 建立一次原阶段并准备只读候选，超时或无候选时保留原模型。
    def _decide(self, thread: object, op: str, mode: str) -> dict:
        agent = self.context.agent
        stage = decision_service.begin_decision_stage(agent, self.params, operation_id=op)
        if stage.error_code or "model_selection" not in stage.enabled_points:
            return {"status": "skipped", "reason": stage.error_code or "disabled"}
        candidates = _candidates(agent, stage.deadline)
        generations = {}
        if mode == "apply":
            from .gateway_model_adoption import freeze_selection_candidates

            candidates, generations = freeze_selection_candidates(agent, candidates, stage.deadline)
        if not candidates:
            return {"status": "skipped", "reason": "no_candidates"}
        state, questions = _observation_input(self.context, thread, self.captured, candidates)
        if mode == "apply":
            questions["model"]["instructions"] = (
                "根据当前任务对已授权目录提出一项模型建议。宿主将在完整首请求准备后独立核对容量、工具和版本，"
                "只有客观验证通过才自动采用。候选声明不是实际能力证明；资料不足可选择 need_data。"
            )
        revision = hashlib.sha256(decision_json(candidates)).hexdigest()
        GatewayActiveTurnTransition(self.context.request_path, self.context.request_id,
                                    self.context.request["execution_attempt_id"])("submit", lambda: None)
        outcome = decision_service.decide(agent, self.params, stage, point="model_selection", state=state,
                                         questions=questions, candidates_revision=revision, caller_deadline=stage.deadline)
        result = _observation_result(outcome, candidates)
        choice = result.get("choice")
        if outcome.may_apply and choice in generations and choice != self.captured.profile_id:
            from .gateway_model_adoption import GatewayModelAdoption

            self.adoption = GatewayModelAdoption(self, thread, stage, outcome, generations[choice])
        return {**result, "candidates_revision": revision}

    # LLM: 原 runner 发布 runtime authority 后由其 finalizer 收口；准备阶段失败尚无运行身份时只沿同一 finalizer 保存本次真实用量。
    # 函数用途: 防止观察后历史/Compact 准备失败丢失 token 统计；不创建第二账本或给 off 请求增加读写。
    def settle_preparation_failure(self) -> None:
        if self.params is None or self.context.request.get("runtime_authority"):
            return
        from .agent_core._finalization_service import FinalizationService

        try:
            FinalizationService(self.context.agent).settle_model_usage(self.params)
        except Exception:
            logging.getLogger(__name__).error("模型观察用量收口失败，保留原请求错误", exc_info=False)
