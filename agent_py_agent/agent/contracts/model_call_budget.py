# LLM: 预算只挂在 ModelCallLedger 原累计容器并共用原锁；此模块没有独立存储、计时循环、网络或授权来源。
# 模块用途: 提供请求前预留、单次发送许可消费与保守结算原语；输入上界必须是带标签的对象，发送硬门在传输层调用。
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace

_BOUND_KINDS = frozenset({"empirical"})
_TERMINAL_STATUSES = frozenset({"failed", "finished", "timed_out"})
# 结算关闭原因只能覆盖仍开放或仅被用户撤销的预算；一份预算同一时刻只有一个待结算调用，不会出现其它状态。
_CLOSABLE_STATUSES = frozenset({"active", "revoked"})
_RATIO_WARNING = 0.8
_SHA256 = re.compile(r"[0-9a-f]{64}")


# LLM: 固定代码是预算裁决事实，不能读取错误文案猜授权、取消或重试；普通模型请求不会使用此异常。
# 类用途: 返回实验预算原语的明确拒绝原因。
class ModelCallBudgetError(ValueError):
    # LLM: reason 由本模块固定分支提供，无网络及持久副作用。
    # 函数用途: 保存可供上层结构化报告的预算拒绝代码。
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# LLM: 上界是调用方按版本化方法算出的宿主声明，kind 标明证据等级；empirical 只是用户接受的经验值，不冒充供应商保证。
# 类用途: 携带完整输入 token 上界及其方法和原始计量，裸整数不能进入预留，估算与实际另记。
@dataclass(frozen=True)
class InputTokenBound:
    tokens: int
    method: str
    body_bytes: int
    questions: int
    state_bytes: int
    kind: str = "empirical"

    # LLM: 只做类型/范围校验，不重新计算方法；未知 kind 或空方法说明来源不明，必须拒绝而不是按经验值放行。
    # 函数用途: 拒绝布尔、负数、超大值和未标注的上界。
    def __post_init__(self) -> None:
        counts = (self.tokens, self.body_bytes, self.questions, self.state_bytes)
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in counts) or self.tokens <= 0:
            raise ModelCallBudgetError("input_bound_invalid")
        if type(self.kind) is not str or self.kind not in _BOUND_KINDS:
            raise ModelCallBudgetError("input_bound_unlabeled")
        if type(self.method) is not str or not self.method or len(self.method) > 128:
            raise ModelCallBudgetError("input_bound_unlabeled")

    # LLM: 记录进入原调用 metadata，只含计量与方法，不含请求正文。
    # 函数用途: 生成可审计的上界快照，供预留、结算和诊断共用。
    def to_record(self) -> dict:
        return {"tokens": self.tokens, "kind": self.kind, "method": self.method, "body_bytes": self.body_bytes,
                "questions": self.questions, "state_bytes": self.state_bytes}


# LLM: 绑定在预留时冻结并写入原记录；端点只存摘要，连接版本是进程内 HMAC，不含密钥或自定义头原文。
# 类用途: 描述这次预留唯一允许发送的最终请求：POST、端点、正文摘要、模型和连接代次。
@dataclass(frozen=True)
class SendPermitBinding:
    endpoint: str
    body_sha256: str
    model: str
    connection_revision: str
    method: str = "POST"

    # LLM: 字段必须完整；摘要只接受小写 sha256 十六进制，避免把空值或别名当作匹配。
    # 函数用途: 在预留或消费前拒绝不完整的发送绑定。
    def __post_init__(self) -> None:
        texts = (self.endpoint, self.model, self.connection_revision)
        if self.method != "POST" or any(type(value) is not str or not value or len(value) > 4096 for value in texts):
            raise ModelCallBudgetError("send_binding_invalid")
        if type(self.body_sha256) is not str or _SHA256.fullmatch(self.body_sha256) is None:
            raise ModelCallBudgetError("send_binding_invalid")

    # LLM: 持久比较字段固定为这五项；端点以摘要保存，避免把自定义地址原文写进调用明细。
    # 函数用途: 生成原记录中的发送绑定，消费时逐项比较。
    def to_record(self) -> dict:
        return {"method": self.method, "endpoint_sha256": hashlib.sha256(self.endpoint.encode("utf-8")).hexdigest(),
                "body_sha256": self.body_sha256, "model": self.model, "connection_revision": self.connection_revision}


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
# 类用途: 保存有限授权的保守占用、上界类型、最近实际/上界比例及串行中的准确 call_id。
@dataclass
class ModelCallInputBudgetState:
    limits: ModelCallInputBudget
    status: str = "active"
    reserved_http_requests: int = 0
    charged_input_tokens: int = 0
    provider_input_tokens: int = 0
    unknown_usage_calls: int = 0
    active_call_id: str = ""
    input_bound_kind: str = ""
    input_bound_ratio: float | None = None
    input_bound_warning: str = ""

    # LLM: 返回独立副本，未知占用和 provider 实际输入分开；到期只作只读投影，不重置余额。
    # 函数用途: 给宿主显示原预算剩余范围、上界类型及关闭原因。
    def snapshot(self, now: float) -> dict:
        return {"budget_id": self.limits.budget_id, "ledger_id": self.limits.ledger_id,
                "status": "expired" if self.status == "active" and now >= self.limits.deadline else self.status,
                "deadline": self.limits.deadline, "max_http_requests": self.limits.max_http_requests,
                "max_input_tokens": self.limits.max_input_tokens,
                "reserved_http_requests": self.reserved_http_requests, "charged_input_tokens": self.charged_input_tokens,
                "provider_input_tokens": self.provider_input_tokens, "unknown_usage_calls": self.unknown_usage_calls,
                "active_call_id": self.active_call_id, "input_bound_kind": self.input_bound_kind,
                "input_bound_ratio": self.input_bound_ratio, "input_bound_warning": self.input_bound_warning}


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

    # LLM: 只接受 InputTokenBound 与 SendPermitBinding；裸整数、估算或字节数不能预留。一次预留对应一次传输且签发单次发送许可。
    # 函数用途: 在同一原锁内核对范围/时间/次数/input 并建立原调用记录，保证并发不能各自看到旧余额。
    def reserve_input_budget(self, params, *, budget_id: str, input_bound: InputTokenBound,
                             send_binding: SendPermitBinding):
        if not isinstance(input_bound, InputTokenBound):
            raise ModelCallBudgetError("input_bound_unlabeled")
        if not isinstance(send_binding, SendPermitBinding):
            raise ModelCallBudgetError("send_binding_invalid")
        with self._lock:
            state = self._require_input_budget(budget_id)
            _check_reservation(state, params, input_bound.tokens, float(self.context.now()))
            if params.call_id in self._index:
                raise ModelCallBudgetError("call_already_reserved")
            metadata = {**params.metadata, "input_budget_id": budget_id, "input_bound": input_bound.to_record(),
                        "input_budget_state": "reserved", "send_permit": {"state": "issued", **send_binding.to_record()}}
            record, retention = self.started_retained(replace(params, metadata=metadata))
            state.active_call_id = record.call_id
            state.reserved_http_requests += 1
            state.charged_input_tokens += input_bound.tokens
            state.input_bound_kind = input_bound.kind
            return record, retention

    # LLM: 发送硬门的账本一步：同锁复核预留仍属此调用、预算仍开放、绑定逐项相等后置为 consumed；只能成功一次。
    # 函数用途: 在真正建立连接前消费单次发送许可，任何不符都抛固定代码且不改变占用。
    def consume_send_permit(self, call_id: str, *, budget_id: str, binding: SendPermitBinding) -> None:
        if not isinstance(binding, SendPermitBinding):
            raise ModelCallBudgetError("send_binding_invalid")
        with self._lock:
            state = self._require_input_budget(budget_id)
            record = self._records[self._index[call_id]] if call_id in self._index else None
            _check_send_permit(state, record, binding, float(self.context.now()))
            permit = {**record.metadata["send_permit"], "state": "consumed"}
            self._replace(replace(record, metadata={**record.metadata, "send_permit": permit}))

    # LLM: 只用原记录的 provider 完整输入结算；门拒绝不退款，失败/超时/缺报/多次 HTTP 保留占用并关闭后续准入，迟到结果不重开终态。
    # 函数用途: 成功时释放上界与真实输入的差值；未知结果不变成零，不按缓存输入扣减。
    def settle_input_budget(self, call_id: str) -> dict:
        with self._lock:
            record = self._require_record(call_id)
            state = self._require_input_budget(record.metadata.get("input_budget_id", ""))
            if record.metadata.get("input_budget_state") != "reserved":
                return state.snapshot(float(self.context.now()))
            if record.status not in _TERMINAL_STATUSES:
                raise ModelCallBudgetError("call_not_terminal")
            if state.active_call_id != call_id:
                raise ModelCallBudgetError("reservation_mismatch")
            outcome = _settle_reservation(state, record)
            self._replace(replace(record, metadata={**record.metadata, "input_budget_state": "settled", **outcome}))
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


# LLM: 调用者持原锁；只读原记录与预算事实，不从请求正文推断；已终结、已有 HTTP 或已消费的许可都不能再次放行。
# 函数用途: 核对单次发送许可的全部前提，失败抛固定拒绝代码。
def _check_send_permit(state: ModelCallInputBudgetState, record, binding: SendPermitBinding, now: float) -> None:
    metadata = record.metadata if record is not None else {}
    if (record is None or state.active_call_id != record.call_id or metadata.get("input_budget_id") != state.limits.budget_id
            or metadata.get("input_budget_state") != "reserved" or record.status in _TERMINAL_STATUSES):
        raise ModelCallBudgetError("reservation_not_active")
    if state.status != "active":
        raise ModelCallBudgetError("budget_" + state.status)
    if now >= state.limits.deadline:
        raise ModelCallBudgetError("budget_expired")
    permit = metadata.get("send_permit")
    if not isinstance(permit, dict) or permit.get("state") != "issued" or record.provider_attempt_count:
        raise ModelCallBudgetError("send_permit_consumed")
    if {key: value for key, value in permit.items() if key != "state"} != binding.to_record():
        raise ModelCallBudgetError("send_binding_mismatch")


# LLM: 只处理原记录中的字段来源；预留不是实际 usage。无许可的 HTTP 视为绕过，未消费且零 HTTP 视为发送前拒绝，均不退款。
# 函数用途: 对一次真实终态保守结算，返回写回原记录的结算结果字段。
def _settle_reservation(state: ModelCallInputBudgetState, record) -> dict:
    permit = record.metadata.get("send_permit")
    consumed = isinstance(permit, dict) and permit.get("state") == "consumed"
    fields = record.provider_usage_fields
    reported = record.provider_usage_reported if fields is None else "input_tokens" in fields
    state.active_call_id = ""
    if record.provider_attempt_count and not consumed:
        return _close(state, "gate_bypassed", unknown=True)
    if not consumed:
        return _close(state, "send_refused", outcome="refused_before_send")
    if record.status == "finished" and record.provider_attempt_count == 1 and reported:
        return _charge(state, record.accounted_input_tokens, int(record.metadata["input_bound"]["tokens"]))
    return _close(state, "usage_unknown", unknown=True)


# LLM: 关闭只推进开放或已撤销的预算；未知结果额外计数，永不释放已占用的 HTTP 或上界。
# 函数用途: 以固定原因关闭预算并返回写回原调用记录的结算结果。
def _close(state: ModelCallInputBudgetState, reason: str, *, outcome: str = "", unknown: bool = False) -> dict:
    if unknown:
        state.unknown_usage_calls += 1
    if state.status in _CLOSABLE_STATUSES:
        state.status = reason
    return {"input_budget_outcome": outcome or reason}


# LLM: 实际值只来自 provider 完整输入；超过上界沿 E1 input_bound_violated 关闭，比例超过 0.8 只作警告不改变准入。
# 函数用途: 用供应商实际输入替换预留上界并记录实际/上界比例。
def _charge(state: ModelCallInputBudgetState, actual: int, bound: int) -> dict:
    state.provider_input_tokens += actual
    state.charged_input_tokens += actual - bound
    ratio = round(actual / bound, 6)
    state.input_bound_ratio = ratio
    state.input_bound_warning = "input_bound_ratio_high" if ratio > _RATIO_WARNING else ""
    if actual > bound:
        return {**_close(state, "input_bound_violated"), "input_bound_ratio": ratio}
    return {"input_budget_outcome": "charged", "input_bound_ratio": ratio}
