# LLM: 发送许可属于原决策服务：只在传输层实际发送前被调用，按固定顺序复核后在原账锁内单次消费；
# 许可不授予业务写入或采用权，任何失败都抛 ProviderSendRefused（用户中断仍抛 InterruptedError）。
# 模块用途: 把一次已预留的实验决策调用绑定到最终请求字节、端点、模型、连接代次和当前授权，拒绝绕过预算的发送。
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..backends.provider_send_gate import ProviderSendAttempt, ProviderSendRefused
from ..concurrency.interrupt import InterruptHandle, is_interrupted
from ..contracts.model_call_budget import ModelCallBudgetError, SendPermitBinding
from ..contracts.model_call_ledger import ModelCallLedger
from ..runtime_context import (
    current_subagent_attempt_id,
    current_subagent_run_id,
    current_task_attributes,
    restore_current_subagent_context,
    set_current_subagent_context,
)


# LLM: 由决策服务在同一准入快照上构造：授权编号、线程、接入点、策略版本和连接版本都不从请求正文或模型输出读取。
# 类用途: 描述一次只观察的实验调用在原预算和发送许可中需要复核的固定事实。
@dataclass(frozen=True)
class DecisionExperimentCall:
    authorization_id: str
    thread_id: str
    point: str
    policy_revision: str
    connection_revision: str


# LLM: 只由 issue_send_permit 在调用线程构造；runner 上下文快照随许可带到发送线程，复核后恢复，不改变其它线程状态。
# 类用途: 传输层 send_permit 的实际实现，admit 通过即在原账本中消费单次发送资格。
@dataclass(frozen=True)
class DecisionSendPermit:
    agent: object = field(repr=False)
    params: object = field(repr=False)
    ledger: ModelCallLedger = field(repr=False)
    call_id: str
    experiment: DecisionExperimentCall
    binding: SendPermitBinding
    deadline: float
    handle: InterruptHandle | None = field(default=None, repr=False)
    runner_context: tuple = field(default=("", "", None), repr=False)

    # LLM: 顺序固定：静态绑定→运行中断/设置撤销/期限→复读设置与准入及连接→原账锁内消费；前一步失败不执行后续。
    # 函数用途: 在实际建立连接前决定本次请求能否发送，拒绝时抛带固定代码的 ProviderSendRefused。
    def admit(self, attempt: ProviderSendAttempt) -> None:
        self._check_binding(attempt)
        self._check_active()
        self._check_current()
        try:
            self.ledger.consume_send_permit(self.call_id, budget_id=self.experiment.authorization_id, binding=self.binding)
        except ModelCallBudgetError as exc:
            raise ProviderSendRefused(exc.reason) from exc

    # LLM: 只比较最终 urllib 请求事实；首个物理发送之外的 attempt 一律拒绝，许可不能被重试复用。
    # 函数用途: 核对方法、端点、正文摘要、模型和发送序号与预留时冻结的绑定一致。
    def _check_binding(self, attempt: ProviderSendAttempt) -> None:
        facts = ((attempt.method, self.binding.method, "send_method_mismatch"),
                 (attempt.url, self.binding.endpoint, "send_endpoint_mismatch"),
                 (attempt.body_sha256, self.binding.body_sha256, "send_body_mismatch"),
                 (attempt.model, self.binding.model, "send_model_mismatch"))
        for actual, expected, code in facts:
            if actual != expected:
                raise ProviderSendRefused(code)
        if attempt.attempt != 0:
            raise ProviderSendRefused("send_retry_forbidden")

    # LLM: 用户中断保持原 InterruptedError 语义；设置撤销句柄与绝对期限到达都在连接前拒绝，不延长期限。
    # 函数用途: 复核发送时刻的停止、设置取消和决策期限。
    def _check_active(self) -> None:
        if is_interrupted():
            raise InterruptedError("当前决策实验已被用户停止。")
        if self.handle is not None and self.handle.cancelled:
            raise ProviderSendRefused("settings_changed")
        if time.monotonic() >= self.deadline:
            raise ProviderSendRefused("deadline_exhausted")

    # LLM: 在发送线程临时恢复调用线程的 runner 身份再复读设置；复读异常（锁忙、坏配置）一律拒绝发送。
    # 函数用途: 按当前设置重跑实验准入并比较策略与连接版本，任何变化都拒绝。
    def _check_current(self) -> None:
        from .decision_experiment import experiment_current

        run_id, attempt_id, attributes = self.runner_context
        previous = set_current_subagent_context(self.agent, run_id=run_id, attempt_id=attempt_id, task_attributes=attributes)
        try:
            reason = experiment_current(self.agent, self.params, thread_id=self.experiment.thread_id, point=self.experiment.point,
                                        revision=self.experiment.policy_revision, connection=self.binding.connection_revision)
        except (InterruptedError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise ProviderSendRefused("experiment_state_unavailable") from exc
        finally:
            restore_current_subagent_context(self.agent, previous)
        if reason:
            raise ProviderSendRefused(reason)


# LLM: 必须在预留成功后的调用线程执行，捕获该线程 runner 身份；本函数不消费预算、不联网。
# 函数用途: 为一次已预留的实验调用签发只能使用一次的发送许可对象。
def issue_send_permit(agent: object, params: object, *, ledger: ModelCallLedger, call_id: str,
                      experiment: DecisionExperimentCall, binding: SendPermitBinding, deadline: float,
                      handle: InterruptHandle | None) -> DecisionSendPermit:
    snapshot = (current_subagent_run_id(agent), current_subagent_attempt_id(agent), current_task_attributes(agent))
    return DecisionSendPermit(agent, params, ledger, call_id, experiment, binding, deadline, handle, snapshot)


__all__ = ["DecisionExperimentCall", "DecisionSendPermit", "issue_send_permit"]
