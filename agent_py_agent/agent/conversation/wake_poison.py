# LLM: 唤醒毒丸判定的纯函数层。把一次领取尝试的结构化结果归成"不计数 / 计数失败 / 只重投失败 / 成功"，
#   维护同因连续段、总次数和两类退避，并判定是否结案。只读结构化事实：admission 码、异常类型与 error_code 属性、
#   报告的 wake_handled 与"是否已有冻结交付"；不读错误文案或模型正文，不做 IO、不读配置。
#   常量是防死循环的安全兜底，不进用户配置。未知 admission 与未知异常类型默认计数（宁可多等几轮后停下，
#   也不无限重试）；瞬时类只按类型排除。改动时联查 docs/design/WAKE_POISON_PILL.md、尝试账存储与 test_wake_poison。
# 模块用途: 判断同一条后台唤醒是否在以同一原因反复失败，给出下次最早可再试的时间，以及何时该结案不再领取。
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields, replace

# 同一原因连续失败到这个次数就结案。比进度策略的 3 次宽，给类型没覆盖到的短暂竞态留余量；
# 7b83c8730 那类程序错误最多被领取 5 次，而不是每 30 秒一次直到有人发现。
WAKE_POISON_SAME_CAUSE_LIMIT = 5
# 不要求同因的总上限：防止两个原因交替出现、逃过连续判定。
WAKE_POISON_TOTAL_LIMIT = 12
# 计数失败后的退避：第 k 次后等 min(30·2^(k−1), 300) 秒，同因 5 次从首次失败到结案约 7.5 分钟。
WAKE_POISON_BACKOFF_BASE_SECONDS = 30.0
WAKE_POISON_BACKOFF_MAX_SECONDS = 300.0
# 只重投路径（已有冻结交付、不调模型）单独退避：便宜但不能无限循环；封顶 15 分钟，渠道恢复后最多再等 15 分钟。
WAKE_REDELIVERY_BACKOFF_BASE_SECONDS = 30.0
WAKE_REDELIVERY_BACKOFF_MAX_SECONDS = 900.0
# 从第一次重投失败起超过 24 小时仍送不出去就结案；足够覆盖一次渠道长时间故障或人工处理。
WAKE_REDELIVERY_GIVE_UP_SECONDS = 86400.0

WAKE_VERDICT_NEUTRAL = "neutral"
WAKE_VERDICT_FAILURE = "failure"
WAKE_VERDICT_REDELIVERY_FAILURE = "redelivery_failure"
WAKE_VERDICT_SUCCESS = "success"

WAKE_REASON_RUN_NO_REPORT = "run:no_report"
WAKE_REASON_DELIVERY_NOT_COMMITTED = "run:delivery_not_committed"
WAKE_REASON_ATTEMPT_ABANDONED = "attempt:abandoned"
WAKE_REASON_CHANNEL_UNAVAILABLE = "delivery:channel_unavailable"

# 领到租约但按预期等待、不算失败的 admission：用户中断、任务已终态、等恢复、来源已消费或已变化。
# 这是已知码的优化清单；不在清单里的码（包括以后新增的）一律计数。
WAKE_EXPECTED_WAIT_ADMISSIONS = frozenset({
    "turn_interrupted",
    "terminal_task_link",
    "authority_recovery_required",
    "wake_source_not_pending",
    "wake_source_changed",
})
# SQLite 主结果码 SQLITE_BUSY=5、SQLITE_LOCKED=6（扩展码的低 8 位）；锁冲突属于瞬时类。
_SQLITE_LOCK_PRIMARY_CODES = frozenset({5, 6})


# LLM: kind 取 WAKE_VERDICT_* 之一；reason_code 只在计数失败与只重投失败时非空，是跨尝试比较"是否同因"的唯一依据。
# 类用途: 一次领取尝试的结构化判定结果。
@dataclass(frozen=True)
class WakeAttemptVerdict:
    kind: str
    reason_code: str = ""


WAKE_ATTEMPT_NEUTRAL = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL)
WAKE_ATTEMPT_SUCCESS = WakeAttemptVerdict(WAKE_VERDICT_SUCCESS)
WAKE_ATTEMPT_ABANDONED = WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_ATTEMPT_ABANDONED)


# LLM: 持久尝试账里的判定状态；字段全为结构化计数与时间戳，from_dict 严格校验类型，坏值抛 ValueError 交存储层转数据损坏。
# 类用途: 保存一条唤醒的同因连续段、总失败次数、只重投失败次数和下次最早可再试的时间。
@dataclass(frozen=True)
class WakePoisonState:
    reason_code: str = ""
    same_cause_count: int = 0
    total_count: int = 0
    first_failed_at: float = 0.0
    last_failed_at: float = 0.0
    redelivery_failures: int = 0
    first_redelivery_failed_at: float = 0.0
    last_redelivery_failed_at: float = 0.0
    next_attempt_at: float = 0.0

    # 函数用途: 转成可写盘的普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    # LLM: 只接受本类字段；计数必须是非负 int（bool 不算），时间必须是非负数，reason_code 必须是字符串。
    # 函数用途: 从尝试账读回状态，形状不对时抛 ValueError。
    @classmethod
    def from_dict(cls, data: object) -> WakePoisonState:
        if not isinstance(data, dict):
            raise ValueError("wake poison state must be an object")
        values: dict[str, object] = {}
        for item in fields(cls):
            value = data.get(item.name, item.default)
            values[item.name] = _checked_field(item.name, value, item.default)
        return cls(**values)


# LLM: same_cause_count / total_count 描述结案时的计数；只重投结案时 redelivery_failures 为重投失败次数。
# 类用途: 表示一条唤醒已达上限、应当结案，以及结案的结构化原因。
@dataclass(frozen=True)
class QuarantineDecision:
    reason_code: str
    same_cause_count: int
    total_count: int
    redelivery_failures: int
    mixed_causes: bool

    # 函数用途: 转成写进结案记录的普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: admission 必须非空；预期等待类不计数，其它任何码计为 admission:<码>，不需要登记新码。
# 函数用途: 判定"领到租约但没执行"这次尝试。
def verdict_for_admission(admission: str) -> WakeAttemptVerdict:
    code = str(admission or "").strip()
    if not code:
        raise ValueError("admission code is required")
    if code in WAKE_EXPECTED_WAIT_ADMISSIONS:
        return WAKE_ATTEMPT_NEUTRAL
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, f"admission:{code}")


# LLM: 瞬时类只按类型排除；其余异常计数，优先用结构化 error_code 属性，没有时用 runtime_error_report 的
#   category 加异常类名。绝不读异常消息。非 Exception 的 BaseException（进程退出、键盘中断）不计数，
#   进程中途死亡由尝试账的 in_flight 标记另行识别。
# 函数用途: 判定"执行中抛异常"这次尝试。
def verdict_for_error(error: BaseException) -> WakeAttemptVerdict:
    if not isinstance(error, Exception) or _is_transient(error):
        return WAKE_ATTEMPT_NEUTRAL
    code = getattr(error, "error_code", "")
    if isinstance(code, str) and code.strip():
        return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, f"error:{code.strip()}")
    from ..runtime_errors import runtime_error_report

    category = str(runtime_error_report(error).get("category") or "unknown")
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, f"error:{category}:{type(error).__name__}")


# LLM: report 为 None 只在"确实执行了、却没出报告"时传入；delivery_frozen 表示这次之后唤醒上已有冻结交付
#   （下次只重投、不调模型），此时失败走只重投规则，不计入同因与总次数。
# 函数用途: 判定"执行完"这次尝试。
def verdict_for_report(report: object | None, *, delivery_frozen: bool) -> WakeAttemptVerdict:
    if report is None:
        return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_RUN_NO_REPORT)
    if getattr(report, "wake_handled", False) is True:
        return WAKE_ATTEMPT_SUCCESS
    if delivery_frozen:
        return WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_DELIVERY_NOT_COMMITTED)


# LLM: 成功清空；不计数的结果原样返回（既不累加也不打断连续段）；计数失败换了原因就从 1 重新计，总次数照加；
#   只重投失败只动重投字段。next_attempt_at 按本次结果的退避重算。
# 函数用途: 按一次尝试的判定推进状态，不修改传入对象。
def next_poison_state(state: WakePoisonState, verdict: WakeAttemptVerdict, *, now: float) -> WakePoisonState:
    if verdict.kind == WAKE_VERDICT_SUCCESS:
        return WakePoisonState()
    if verdict.kind == WAKE_VERDICT_FAILURE:
        same = state.same_cause_count + 1 if verdict.reason_code == state.reason_code else 1
        total = state.total_count + 1
        return replace(state, reason_code=verdict.reason_code, same_cause_count=same, total_count=total,
                       first_failed_at=now if state.total_count == 0 else state.first_failed_at, last_failed_at=now,
                       next_attempt_at=now + poison_backoff_seconds(total))
    if verdict.kind == WAKE_VERDICT_REDELIVERY_FAILURE:
        failures = state.redelivery_failures + 1
        return replace(state, redelivery_failures=failures,
                       first_redelivery_failed_at=now if state.redelivery_failures == 0 else state.first_redelivery_failed_at,
                       last_redelivery_failed_at=now, next_attempt_at=now + redelivery_backoff_seconds(failures))
    return state


# LLM: 判定顺序固定：同因达上限 → 总次数达上限（标 mixed_causes）→ 只重投失败跨度满 24 小时；都不满足返回 None。
# 函数用途: 判断当前状态是否应当结案。
def quarantine_decision(state: WakePoisonState) -> QuarantineDecision | None:
    if state.same_cause_count >= WAKE_POISON_SAME_CAUSE_LIMIT:
        return _decision(state, state.reason_code, mixed_causes=False)
    if state.total_count >= WAKE_POISON_TOTAL_LIMIT:
        return _decision(state, state.reason_code, mixed_causes=True)
    if state.redelivery_failures and (
            state.last_redelivery_failed_at - state.first_redelivery_failed_at >= WAKE_REDELIVERY_GIVE_UP_SECONDS):
        return _decision(state, WAKE_REASON_CHANNEL_UNAVAILABLE, mixed_causes=False)
    return None


# 函数用途: 计数失败 n 次后的退避秒数：30、60、120、240，之后封顶 300。
def poison_backoff_seconds(failures: int) -> float:
    return _exponential(failures, WAKE_POISON_BACKOFF_BASE_SECONDS, WAKE_POISON_BACKOFF_MAX_SECONDS)


# 函数用途: 只重投失败 n 次后的退避秒数：从 30 秒翻倍，封顶 900 秒。
def redelivery_backoff_seconds(failures: int) -> float:
    return _exponential(failures, WAKE_REDELIVERY_BACKOFF_BASE_SECONDS, WAKE_REDELIVERY_BACKOFF_MAX_SECONDS)


# 函数用途: 通用封顶指数退避；n 小于 1 时不等待。
def _exponential(failures: int, base: float, cap: float) -> float:
    if failures < 1:
        return 0.0
    return min(base * (2 ** min(failures - 1, 32)), cap)


# 函数用途: 由状态和结构化原因组装结案判定。
def _decision(state: WakePoisonState, reason_code: str, *, mixed_causes: bool) -> QuarantineDecision:
    return QuarantineDecision(reason_code=reason_code, same_cause_count=state.same_cause_count,
                              total_count=state.total_count, redelivery_failures=state.redelivery_failures,
                              mixed_causes=mixed_causes)


# LLM: 只按类型判定：供应瞬时（含限流）、供应超时、额度耗尽、模型配置暂缺、运行库冲突全族（含锁冲突与待恢复）、
#   取消与压缩让出、本地 IO 的阻塞与超时、SQLite 忙或锁。新增瞬时类型要在这里显式加，不能靠消息匹配。
# 函数用途: 判断一个异常是否属于不计数的瞬时类。
def _is_transient(error: Exception) -> bool:
    from ..backends.errors import (
        ProviderTimeoutError,
        is_provider_quota_exhausted_error,
        is_provider_transient_error,
    )
    from ..runtime_db.operations import RuntimeConflictError
    from ..settings.thread_model_selection import is_model_configuration_unavailable
    from .background_execution import BackgroundCompactSliceYield

    if isinstance(error, (InterruptedError, BlockingIOError, TimeoutError, ProviderTimeoutError,
                          RuntimeConflictError, BackgroundCompactSliceYield)):
        return True
    if is_provider_transient_error(error) or is_provider_quota_exhausted_error(error):
        return True
    if is_model_configuration_unavailable(error):
        return True
    return isinstance(error, sqlite3.OperationalError) and _sqlite_lock_code(error)


# 函数用途: 读 sqlite3 异常的结构化结果码（Python 3.11 起才有）；没有这个属性时返回 False，按计数处理。
def _sqlite_lock_code(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return type(code) is int and (code & 0xFF) in _SQLITE_LOCK_PRIMARY_CODES


# 函数用途: 按字段默认值的类型校验一个状态字段。
def _checked_field(name: str, value: object, default: object) -> object:
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"wake poison state field {name} must be a string")
        return value
    if isinstance(default, int):
        if type(value) is not int or value < 0:
            raise ValueError(f"wake poison state field {name} must be a non-negative integer")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"wake poison state field {name} must be a non-negative number")
    return float(value)


__all__ = [
    "WAKE_ATTEMPT_ABANDONED",
    "WAKE_EXPECTED_WAIT_ADMISSIONS",
    "WAKE_ATTEMPT_NEUTRAL",
    "WAKE_REASON_ATTEMPT_ABANDONED",
    "WAKE_REASON_CHANNEL_UNAVAILABLE",
    "WAKE_REASON_DELIVERY_NOT_COMMITTED",
    "WAKE_REASON_RUN_NO_REPORT",
    "WAKE_ATTEMPT_SUCCESS",
    "WAKE_VERDICT_FAILURE",
    "WAKE_VERDICT_NEUTRAL",
    "WAKE_VERDICT_REDELIVERY_FAILURE",
    "WAKE_VERDICT_SUCCESS",
    "WAKE_POISON_BACKOFF_BASE_SECONDS",
    "WAKE_POISON_BACKOFF_MAX_SECONDS",
    "WAKE_POISON_SAME_CAUSE_LIMIT",
    "WAKE_POISON_TOTAL_LIMIT",
    "WAKE_REDELIVERY_BACKOFF_BASE_SECONDS",
    "WAKE_REDELIVERY_BACKOFF_MAX_SECONDS",
    "WAKE_REDELIVERY_GIVE_UP_SECONDS",
    "QuarantineDecision",
    "WakeAttemptVerdict",
    "WakePoisonState",
    "next_poison_state",
    "poison_backoff_seconds",
    "quarantine_decision",
    "redelivery_backoff_seconds",
    "verdict_for_admission",
    "verdict_for_error",
    "verdict_for_report",
]
