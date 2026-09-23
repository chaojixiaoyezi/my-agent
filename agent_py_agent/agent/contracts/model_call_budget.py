# LLM: 预算只挂在 ModelCallLedger 原累计容器并共用原锁；此模块没有独立存储、计时循环、网络或授权来源。
# 模块用途: 提供请求前预留与保守结算原语；调用者仍须证明完整实际载荷输入上界并在真实传输前接入硬门。
from __future__ import annotations

import math
from dataclasses import dataclass, replace


# LLM: 固定代码是预算裁决事实，不能读取错误文案猜授权、取消或重试；普通模型请求不会使用此异常。
# 类用途: 返回实验预算原语的明确拒绝原因。
class ModelCallBudgetError(ValueError):
    # LLM: reason 由本模块固定分支提供，无网络及持久副作用。
    # 函数用途: 保存可供上层结构化报告的预算拒绝代码。
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# LLM: 宿主从原授权构建一次固定预算；ledger_id 绑定当前原账代次，deadline 是准备前已冻结的 monotonic 期限。
# 类用途: 描述同一原执行片的有限 HTTP/input 预算，不计输出、缓存折扣、价格或费用。
@dataclass(frozen=True)
class ModelCallInputBudget:
    budget_id: str
    ledger_id: str
    owner_ref: str
    thread_id: str
    task_id: str
    run_id: str
    attempt_id: str
    request_id: str
    deadline: float
    max_http_requests: int
    max_input_tokens: int

    # LLM: 固定范围和上限都必须完整，零/布尔/NaN 不转换成无限值；这里只校验，不授予实验执行权。
    # 函数用途: 拒绝不可审计或无限的预算声明。
    def __post_init__(self) -> None:
        ids = (self.budget_id, self.ledger_id, *self.identity)
        if any(type(value) is not str or not value or value != value.strip() or len(value) > 1024 for value in ids):
            raise ModelCallBudgetError("invalid_budget_identity")
        if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in (self.max_http_requests, self.max_input_tokens)):
            raise ModelCallBudgetError("invalid_budget_limit")
        try:
            valid = type(self.deadline) in (int, float) and math.isfinite(self.deadline)
        except OverflowError:
            valid = False
        if not valid:
            raise ModelCallBudgetError("invalid_budget_deadline")

    # LLM: identity 只用于宿主提供的结构化身份逐项比较，不从 prompt 或材料正文提取任务身份。
    # 函数用途: 返回预留时必须一致的准确运行范围。
    @property
    def identity(self) -> tuple[str, ...]:
        return self.owner_ref, self.thread_id, self.task_id, self.run_id, self.attempt_id, self.request_id


# LLM: 这是原累计容器中的预留状态，不是第二账本；只在原 ledger 锁内变更，不能从持久授权重建旧余额。
# 类用途: 保存有限授权的保守占用及串行中的准确 call_id。
@dataclass
class ModelCallInputBudgetState:
    limits: ModelCallInputBudget
    status: str = "active"
    reserved_http_requests: int = 0
    charged_input_tokens: int = 0
    provider_input_tokens: int = 0
    unknown_usage_calls: int = 0
    active_call_id: str = ""

    # LLM: 返回独立副本，未知占用和 provider 实际输入分开；到期只作只读投影，不重置余额。
    # 函数用途: 给宿主显示原预算剩余范围及关闭原因。
    def snapshot(self, now: float) -> dict:
        return {"budget_id": self.limits.budget_id, "ledger_id": self.limits.ledger_id,
                "status": "expired" if self.status == "active" and now >= self.limits.deadline else self.status,
                "deadline": self.limits.deadline, "max_http_requests": self.limits.max_http_requests,
                "max_input_tokens": self.limits.max_input_tokens,
                "reserved_http_requests": self.reserved_http_requests, "charged_input_tokens": self.charged_input_tokens,
                "provider_input_tokens": self.provider_input_tokens, "unknown_usage_calls": self.unknown_usage_calls,
                "active_call_id": self.active_call_id}


# LLM: 只由原 ModelCallLedger 继承；方法访问原锁、原累计容器和原 ModelCallRecord，不拥有旁路数据库或余额缓存。
# 类用途: 扩展原模型账的显式预算操作，普通 started/finished/HTTP 观察路径完全保持原行为。
class ModelCallInputBudgetMethods:
    # LLM: 已裁剪或丢失的原容器不能由授权快照恢复；重新授权必须产生新编号及固定上限。
    # 函数用途: 取得现存预算，缺失时失败关闭而不是重新给额度。
    def _require_input_budget(self, budget_id: str) -> ModelCallInputBudgetState:
        aggregate = self._scope_aggregates.get(("input_budget", budget_id))
        state = getattr(aggregate, "input_budget", None)
        if state is None:
            raise ModelCallBudgetError("budget_missing")
        return state

    # LLM: 此读取不初始化预算或累计容器，不接受别的 ledger 代次，也不改变普通用量口径。
    # 函数用途: 读取同一本账中的实验预留状态。
    def input_budget_snapshot(self, budget_id: str) -> dict:
        with self._lock:
            return self._require_input_budget(budget_id).snapshot(float(self.context.now()))

    # LLM: 撤销与预留共用原锁；旧许可永久失效，重新打开能力开关不会改变本状态或给余额归零。
    # 函数用途: 停止这份原预算的后续预留，保留已可能发送的占用。
    def revoke_input_budget(self, budget_id: str) -> dict:
        with self._lock:
            state = self._require_input_budget(budget_id)
            state.status = "revoked"
            return state.snapshot(float(self.context.now()))

    # LLM: 仅宿主已有可靠完整载荷上界时使用；这里不把 int、估算或字节数当证明。一次预留对应一次传输，当前串行且不自动退费。
    # 函数用途: 在同一原锁内核对范围/时间/次数/input 并建立原调用记录，保证并发不能各自看到旧余额。
    def reserve_input_budget(self, params, *, budget_id: str, input_token_upper_bound: int):
        if type(input_token_upper_bound) is not int or not 0 < input_token_upper_bound <= 2**63 - 1:
            raise ModelCallBudgetError("input_bound_unavailable")
        with self._lock:
            state = self._require_input_budget(budget_id)
            _check_reservation(state, params, input_token_upper_bound, float(self.context.now()))
            if params.call_id in self._index:
                raise ModelCallBudgetError("call_already_reserved")
            metadata = {**params.metadata, "input_budget_id": budget_id, "input_token_upper_bound": input_token_upper_bound,
                        "input_budget_state": "reserved"}
            record, retention = self.started_retained(replace(params, metadata=metadata))
            state.active_call_id = record.call_id
            state.reserved_http_requests += 1
            state.charged_input_tokens += input_token_upper_bound
            return record, retention

    # LLM: 只用原记录的 provider 完整输入结算；失败/超时/缺报/多次 HTTP 保留占用并关闭后续准入，迟到结果不重开原终态。
    # 函数用途: 成功时释放可靠上界与真实输入的差值；未知结果不变成零，不按缓存输入扣减。
    def settle_input_budget(self, call_id: str) -> dict:
        with self._lock:
            record = self._require_record(call_id)
            state = self._require_input_budget(record.metadata.get("input_budget_id", ""))
            if record.metadata.get("input_budget_state") != "reserved":
                return state.snapshot(float(self.context.now()))
            if record.status not in {"finished", "failed", "timed_out"}:
                raise ModelCallBudgetError("call_not_terminal")
            if state.active_call_id != call_id:
                raise ModelCallBudgetError("reservation_mismatch")
            _settle_reservation(state, record)
            self._replace(replace(record, metadata={**record.metadata, "input_budget_state": "settled"}))
            return state.snapshot(float(self.context.now()))


# LLM: 全部判断在调用者已持原锁的区间；宿主身份不从调用正文读取，任何失败均在原记录和余额变更之前返回。
# 函数用途: 执行预留的准确身份、串行和有限预算检查。
def _check_reservation(state: ModelCallInputBudgetState, params, upper_bound: int, now: float) -> None:
    identity = tuple(params.metadata.get(key, "") for key in ("owner_ref", "thread_id", "task_id")) + (
        params.run_id, params.metadata.get("attempt_id", ""), params.request_id)
    if identity != state.limits.identity or params.metadata.get("purpose") != "decision":
        raise ModelCallBudgetError("budget_identity_mismatch")
    if state.status != "active":
        raise ModelCallBudgetError("budget_" + state.status)
    if now >= state.limits.deadline:
        raise ModelCallBudgetError("budget_expired")
    if state.active_call_id:
        raise ModelCallBudgetError("budget_busy")
    if state.reserved_http_requests >= state.limits.max_http_requests:
        raise ModelCallBudgetError("http_budget_exhausted")
    if state.charged_input_tokens + upper_bound > state.limits.max_input_tokens:
        raise ModelCallBudgetError("input_budget_exhausted")


# LLM: 只处理原记录中的字段来源；reservation 不是实际 usage，provider 越过上界表示合同失效，不能继续实验或把错误差额退回。
# 函数用途: 对一次真实终态保守结算，UNKNOWN、缺输入和传输计数不符均终止本预算。
def _settle_reservation(state: ModelCallInputBudgetState, record) -> None:
    fields = record.provider_usage_fields
    reported = record.provider_usage_reported if fields is None else "input_tokens" in fields
    bound = record.metadata["input_token_upper_bound"]
    if record.status == "finished" and record.provider_attempt_count == 1 and reported:
        actual = record.accounted_input_tokens
        state.provider_input_tokens += actual
        state.charged_input_tokens += actual - bound
        if actual > bound and state.status == "active":
            state.status = "input_bound_violated"
    else:
        state.unknown_usage_calls += 1
        if state.status == "active":
            state.status = "usage_unknown"
    state.active_call_id = ""
