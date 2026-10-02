# LLM: 唤醒毒丸判定的纯函数层。把一次领取尝试的结构化结果归成"不计数 / 计数失败 / 批次失败 / 只重投失败 / 成功"，
#   维护同因连续段、总次数、批次隔离、连续不计数段和两类退避，并判定是否结案、是否该发"长时间不计数"提醒。
#   只读结构化事实：admission 码、异常类型与 error_code / status_code 属性、报告的 wake_handled 与"是否已有冻结交付"、
#   批大小；不读错误文案或模型正文，不做 IO、不读配置。常量是防死循环的安全兜底，不进用户配置。
#   未知 admission 与未知异常类型默认计数（宁可多等几轮后停下，也不无限重试）；瞬时类与环境级故障只按类型或
#   结构化状态码排除（环境级故障由 Gateway 后台车道按环境暂停，见 cli/gateway_lane_retry）。时间只往前走，时钟回拨时账照样读得回来。改动时联查 docs/design/WAKE_POISON_PILL.md、store_wake_attempts 与 test_wake_poison。
# 模块用途: 判断同一条后台唤醒是否在以同一原因反复失败，给出下次最早可再试的时间，以及何时该结案不再领取。
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, fields, replace

from ..runtime_errors import is_structured_error_code

# 同一原因连续失败到这个次数就结案。比进度策略的 3 次宽，给类型没覆盖到的短暂竞态留余量；
# 7b83c8730 那类程序错误最多被领取 5 次，而不是每 30 秒一次直到有人发现。
WAKE_POISON_SAME_CAUSE_LIMIT_COUNT = 5
# 不要求同因的总上限：防止两个原因交替出现、逃过连续判定。
WAKE_POISON_TOTAL_LIMIT_COUNT = 12
# 计数失败后的退避：第 k 次后等 min(30·2^(k−1), 300) 秒，同因 5 次从首次失败到结案约 7.5 分钟。
# 批次失败后也按基础间隔 30 秒再试，下一次逐条单独执行。
WAKE_POISON_BACKOFF_BASE_SECONDS = 30.0
# 唤醒毒化计数失败后的退避封顶 300 秒：同因 5 次从首次失败到结案约 7.5 分钟。
WAKE_POISON_BACKOFF_MAX_SECONDS = 300.0
# 只重投路径（已有冻结交付、不调模型）单独退避：便宜但不能无限循环；封顶 15 分钟，渠道恢复后最多再等 15 分钟。
WAKE_REDELIVERY_BACKOFF_BASE_SECONDS = 30.0
# 只重投路径（不调模型）单独退避封顶 900 秒（15 分钟）：渠道恢复后最多再等 15 分钟。
WAKE_REDELIVERY_BACKOFF_MAX_SECONDS = 900.0
# 从第一次重投失败起满 24 小时（>=）仍送不出去就结案；足够覆盖一次渠道长时间故障或人工处理。
WAKE_REDELIVERY_GIVE_UP_SECONDS = 86400.0
# 连续不计数满这么久（与只重投路径同一个窗口）就发一次运维提醒，之后每隔同样时长再提醒；不自动结案，
# 因为这些结果按定义属于预期等待、瞬时或环境故障，长时间故障里自动结案会误伤健康的工作。
WAKE_UNCOUNTED_STALL_SECONDS = 86400.0

# 唤醒结案后的终态：不再被领取，只能人工重放；发布层把它当作与 pending/handled 并列的状态。
WAKE_STATUS_FAILED_PERMANENTLY = "failed_permanently"

WAKE_VERDICT_NEUTRAL = "neutral"
WAKE_VERDICT_FAILURE = "failure"
WAKE_VERDICT_BATCH_FAILURE = "batch_failure"
WAKE_VERDICT_REDELIVERY_FAILURE = "redelivery_failure"
WAKE_VERDICT_SUCCESS = "success"

WAKE_REASON_RUN_NO_REPORT = "run:no_report"
WAKE_REASON_DELIVERY_NOT_COMMITTED = "run:delivery_not_committed"
WAKE_REASON_ATTEMPT_ABANDONED = "attempt:abandoned"
WAKE_REASON_CHANNEL_UNAVAILABLE = "delivery:channel_unavailable"
# 第 3 步接线新增的结构化原因（顺序与 docs/design/WAKE_POISON_PILL.md 第 10 节的四点一致）：
# 额度分路的回退通知、心跳执行结算成取消、压缩让出——都不计数；报告说唤醒已处理但它还在 pending——计数；
# 进程带停机标记退出、尝试账读不出——由存储层/接线层按这两个原因记账或结案。
WAKE_REASON_QUOTA_FALLBACK = "quota:fallback_notice"
WAKE_REASON_RUN_CANCELLED = "run:cancelled"
WAKE_REASON_COMPACT_YIELD = "run:compact_yield"
WAKE_REASON_WAKE_NOT_SETTLED = "run:wake_not_settled"
WAKE_REASON_GATEWAY_STOPPED = "attempt:gateway_stopped"
WAKE_REASON_LEDGER_CORRUPT = "attempt:ledger_corrupt"

# 一次尝试走的路径：拿到会话 claim 之后执行；只重投（已有冻结交付，不调模型）；额度分路（只发回退通知）。
WAKE_ATTEMPT_PATH_CLAIMED = "claimed"
WAKE_ATTEMPT_PATH_REDELIVERY = "redelivery"
WAKE_ATTEMPT_PATH_QUOTA_FALLBACK = "quota_fallback"
WAKE_ATTEMPT_PATHS = frozenset({WAKE_ATTEMPT_PATH_CLAIMED, WAKE_ATTEMPT_PATH_REDELIVERY, WAKE_ATTEMPT_PATH_QUOTA_FALLBACK})
# claim 路径的结算状态（background_claim 的 claims.finish status）：领到没执行、执行完、取消、压缩让出。
WAKE_CLAIM_NOT_EXECUTED = "not_executed"
WAKE_CLAIM_FINISHED = "finished"
WAKE_CLAIM_CANCELLED = "cancelled"
WAKE_CLAIM_YIELDED = "yielded"
WAKE_CLAIM_STATUSES = frozenset({WAKE_CLAIM_NOT_EXECUTED, WAKE_CLAIM_FINISHED, WAKE_CLAIM_CANCELLED, WAKE_CLAIM_YIELDED})

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


# LLM: kind 取 WAKE_VERDICT_* 之一；reason_code 是结构化原因，计数失败时是比较"是否同因"的唯一依据，
#   不计数时只用于长时间不计数的提醒。
# 类用途: 一次领取尝试的结构化判定结果。
@dataclass(frozen=True)
class WakeAttemptVerdict:
    kind: str
    reason_code: str = ""


WAKE_ATTEMPT_NEUTRAL = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL)
WAKE_ATTEMPT_SUCCESS = WakeAttemptVerdict(WAKE_VERDICT_SUCCESS)
WAKE_ATTEMPT_ABANDONED = WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_ATTEMPT_ABANDONED)
WAKE_ATTEMPT_GATEWAY_STOPPED = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, WAKE_REASON_GATEWAY_STOPPED)


# LLM: 持久尝试账里的判定状态；字段全为结构化计数、原因码与时间戳，from_dict 严格校验类型与一致性，
#   坏值抛 ValueError 交存储层转数据损坏。batch_failures>0 表示下一次必须逐条单独执行；uncounted_* 描述当前
#   连续不计数段，stall_* 记录这一段已经发过几次提醒。
# 类用途: 保存一条唤醒的同因连续段、总失败次数、批次隔离、连续不计数段、只重投失败和下次最早可再试的时间。
@dataclass(frozen=True)
class WakePoisonState:
    reason_code: str = ""
    same_cause_count: int = 0
    total_count: int = 0
    first_failed_at: float = 0.0
    last_failed_at: float = 0.0
    batch_failures: int = 0
    uncounted_reason_code: str = ""
    uncounted_count: int = 0
    uncounted_since: float = 0.0
    stall_alerts: int = 0
    last_stall_alert_at: float = 0.0
    redelivery_failures: int = 0
    first_redelivery_failed_at: float = 0.0
    last_redelivery_failed_at: float = 0.0
    next_attempt_at: float = 0.0

    # LLM: 字段与写盘键一一对应，from_dict 是它的严格逆操作；新增字段要同步 from_dict 的一致性检查与测试。
    # 函数用途: 转成可写盘的普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    # LLM: 只接受本类字段，未知键按数据损坏处理；计数必须是非负 int（bool 不算），时间必须是有限的非负数，
    #   原因码必须是字符串；再核计数与时间之间的一致性（_check_consistency），矛盾同样抛 ValueError。
    # 函数用途: 从尝试账读回状态，形状不对或自相矛盾时抛 ValueError。
    @classmethod
    def from_dict(cls, data: object) -> WakePoisonState:
        if not isinstance(data, dict):
            raise ValueError("wake poison state must be an object")
        known = {item.name for item in fields(cls)}
        unknown = sorted(str(key) for key in data if key not in known)
        if unknown:
            raise ValueError(f"wake poison state has unknown fields: {unknown}")
        state = cls(**{item.name: _checked_field(item.name, data.get(item.name, item.default), item.default)
                       for item in fields(cls)})
        _check_consistency(state)
        return state


# LLM: same_cause_count / total_count 描述结案时的计数；只重投结案时 redelivery_failures 为重投失败次数。
# 类用途: 表示一条唤醒已达上限、应当结案，以及结案的结构化原因。
@dataclass(frozen=True)
class QuarantineDecision:
    reason_code: str
    same_cause_count: int
    total_count: int
    redelivery_failures: int
    mixed_causes: bool

    # LLM: 结案记录与列表只读这些结构化字段，不带错误消息或正文。
    # 函数用途: 转成写进结案记录的普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: 只描述当前连续不计数段：原因码是最近一次不计数的原因，alert_number 从 1 开始；不含错误消息或正文。
# 类用途: 表示一条唤醒连续不计数已满提醒窗口、需要发运维事件和宿主提示。
@dataclass(frozen=True)
class WakeStallAlert:
    reason_code: str
    uncounted_count: int
    uncounted_since: float
    alert_number: int

    # LLM: 运维事件与宿主提示只读这些结构化字段。
    # 函数用途: 转成普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: admission 必须非空；预期等待类不计数（带 admission:<码> 供提醒使用），其它任何码计为 admission:<码>，不需要登记新码。
# 函数用途: 判定"领到租约但没执行"这次尝试。
def verdict_for_admission(admission: str) -> WakeAttemptVerdict:
    code = str(admission or "").strip()
    if not code:
        raise ValueError("admission code is required")
    kind = WAKE_VERDICT_NEUTRAL if code in WAKE_EXPECTED_WAIT_ADMISSIONS else WAKE_VERDICT_FAILURE
    return WakeAttemptVerdict(kind, f"admission:{code}")


# LLM: 瞬时类与环境级故障只按类型或结构化状态码排除（_is_uncounted），其余异常计数；原因码见 _error_reason_code，
#   绝不读异常消息。非 Exception 的 BaseException（进程退出、键盘中断）不计数，进程中途死亡由尝试账的
#   in_flight 标记另行识别。
# 函数用途: 判定"执行中抛异常"这次尝试。
def verdict_for_error(error: BaseException) -> WakeAttemptVerdict:
    if not isinstance(error, Exception):
        return WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, f"error:base_exception:{type(error).__name__}")
    kind = WAKE_VERDICT_NEUTRAL if _is_uncounted(error) else WAKE_VERDICT_FAILURE
    return WakeAttemptVerdict(kind, _error_reason_code(error))


# LLM: report 必须是真实报告对象（None 抛 ValueError）；"确实执行了、却没出报告"走 verdict_for_missing_report，
#   不能用 None 混表几种情况。delivery_frozen 表示这次之后唤醒上已有冻结交付（下次只重投、不调模型），
#   此时失败走只重投规则，不计入同因与总次数。
# 函数用途: 判定"执行完并拿到报告"这次尝试。
def verdict_for_report(report: object, *, delivery_frozen: bool) -> WakeAttemptVerdict:
    if report is None:
        raise ValueError("report is required; use verdict_for_missing_report for an executed run without one")
    if getattr(report, "wake_handled", False) is True:
        return WAKE_ATTEMPT_SUCCESS
    if delivery_frozen:
        return WakeAttemptVerdict(WAKE_VERDICT_REDELIVERY_FAILURE, WAKE_REASON_CHANNEL_UNAVAILABLE)
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_DELIVERY_NOT_COMMITTED)


# LLM: 只在调用方确认工作片真的执行过、却没有返回报告时使用；没领到租约、领到未执行都另有入口，不能走这里。
# 函数用途: 判定"执行了但没有报告"这次尝试，计为 run:no_report。
def verdict_for_missing_report() -> WakeAttemptVerdict:
    return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_RUN_NO_REPORT)


# LLM: 接线层（wake_attempt_tracking）在一次尝试结束时填好的结构化事实；只有 path=claimed 时 claim_status 有意义。
#   error 是执行路径抛出的异常（含被供应冷却吸收的瞬时异常）；report 是执行返回的报告对象或 None；
#   delivery_frozen 由调用方重读 pending 信封后算出（cached_owner_delivery 非空）。"唤醒已不在 pending → 删账"和
#   "根本没开始尝试 → 不记账"由接线层先判掉，不进本判定。
# 类用途: 描述一次已经结束的唤醒领取尝试，供 verdict_for_attempt 归类。
@dataclass(frozen=True)
class WakeAttemptFacts:
    path: str = WAKE_ATTEMPT_PATH_CLAIMED
    claim_status: str = WAKE_CLAIM_FINISHED
    admission: str = ""
    report: object = None
    error: BaseException | None = None
    delivery_frozen: bool = False


# LLM: 第 3 步的判定总表，顺序固定：① 抛了异常 → verdict_for_error；② 额度分路 → 不计数 quota:fallback_notice；
#   ③ 只重投 → verdict_for_report(delivery_frozen=True)；④ 领到没执行 → verdict_for_admission；⑤ 取消 / 压缩让出 → 不计数；
#   ⑥ 执行完没报告 → run:no_report；⑦ 报告说 wake_handled=True 但唤醒仍在 pending（调用方保证只在 pending 时调用）→ 计数
#   run:wake_not_settled；⑧ 其余 → verdict_for_report(按调用方给的 delivery_frozen)。只读结构化字段，不做 IO；
#   不认识的 path / claim_status 是调用方缺陷，抛 ValueError。改动时同步 docs/design/WAKE_POISON_PILL.md 第 10 节与 test_wake_poison。
# 函数用途: 把一次尝试的结构化事实归成一个判定结果。
def verdict_for_attempt(facts: WakeAttemptFacts) -> WakeAttemptVerdict:
    if facts.path not in WAKE_ATTEMPT_PATHS:
        raise ValueError(f"unknown wake attempt path: {facts.path!r}")
    if facts.error is not None:
        return verdict_for_error(facts.error)
    if facts.path == WAKE_ATTEMPT_PATH_QUOTA_FALLBACK:
        return WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, WAKE_REASON_QUOTA_FALLBACK)
    if facts.path == WAKE_ATTEMPT_PATH_REDELIVERY:
        return verdict_for_report(facts.report, delivery_frozen=True)
    return _verdict_for_claimed_attempt(facts)


# LLM: claim 路径的分支（verdict_for_attempt 的④–⑧），状态词表以 WAKE_CLAIM_STATUSES 为准。
# 函数用途: 判定一次拿到会话 claim 之后的尝试。
def _verdict_for_claimed_attempt(facts: WakeAttemptFacts) -> WakeAttemptVerdict:
    if facts.claim_status not in WAKE_CLAIM_STATUSES:
        raise ValueError(f"unknown wake claim status: {facts.claim_status!r}")
    if facts.claim_status == WAKE_CLAIM_NOT_EXECUTED:
        return verdict_for_admission(facts.admission)
    if facts.claim_status == WAKE_CLAIM_CANCELLED:
        return WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, WAKE_REASON_RUN_CANCELLED)
    if facts.claim_status == WAKE_CLAIM_YIELDED:
        return WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, WAKE_REASON_COMPACT_YIELD)
    if facts.report is None:
        return verdict_for_missing_report()
    if getattr(facts.report, "wake_handled", False) is True:
        return WakeAttemptVerdict(WAKE_VERDICT_FAILURE, WAKE_REASON_WAKE_NOT_SETTLED)
    return verdict_for_report(facts.report, delivery_frozen=facts.delivery_frozen)


# LLM: 批次隔离（消息队列隔离毒消息的常规做法）：一次尝试同时执行了多条唤醒（batch_size>1）时，计数失败改记为
#   批次失败——不计入任何成员的同因与总次数，只让下一次逐条单独执行；只有批大小为 1 的失败才计数。
#   其它 kind 原样返回。batch_size 小于 1 是调用方缺陷，抛 ValueError。
# 函数用途: 按本次尝试的批大小改写判定，避免一条有毒的内容拖着同批健康成员一起结案。
def verdict_for_batch(verdict: WakeAttemptVerdict, batch_size: int) -> WakeAttemptVerdict:
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    if batch_size > 1 and verdict.kind == WAKE_VERDICT_FAILURE:
        return WakeAttemptVerdict(WAKE_VERDICT_BATCH_FAILURE, verdict.reason_code)
    return verdict


# 计数失败、批次失败、只重投失败都必须带结构化原因：同因比较、提醒和结案记录都靠它，空原因写进账就读不回来。
_REASON_REQUIRED_KINDS = frozenset({WAKE_VERDICT_FAILURE, WAKE_VERDICT_BATCH_FAILURE, WAKE_VERDICT_REDELIVERY_FAILURE})


# LLM: 成功清空全部；计数失败推进同因与总次数并结束当前不计数段；不计数与批次失败都推进连续不计数段（既不累加失败，
#   也不打断同因连续段），批次失败另加 batch_failures；只重投失败只动重投字段。next_attempt_at 按本次结果的退避重算
#   （不计数保持原值）。时间只往前走：本机时钟回拨时各段的"最近一次"取 max(now, 已记的最近时间)，账始终自洽可读。
#   未知 kind、需要原因的 kind 却给了空原因，都抛 ValueError，不静默写坏账。
# 函数用途: 按一次尝试的判定推进状态，不修改传入对象。
def next_poison_state(state: WakePoisonState, verdict: WakeAttemptVerdict, *, now: float) -> WakePoisonState:
    if verdict.kind in _REASON_REQUIRED_KINDS and not str(verdict.reason_code or "").strip():
        raise ValueError(f"wake attempt verdict {verdict.kind!r} requires a reason_code")
    if verdict.kind == WAKE_VERDICT_SUCCESS:
        return WakePoisonState()
    if verdict.kind == WAKE_VERDICT_FAILURE:
        return _after_failure(state, verdict.reason_code, now)
    if verdict.kind == WAKE_VERDICT_NEUTRAL:
        return _after_uncounted(state, verdict.reason_code, now)
    if verdict.kind == WAKE_VERDICT_BATCH_FAILURE:
        return replace(_after_uncounted(state, verdict.reason_code, now), batch_failures=state.batch_failures + 1,
                       next_attempt_at=now + WAKE_POISON_BACKOFF_BASE_SECONDS)
    if verdict.kind == WAKE_VERDICT_REDELIVERY_FAILURE:
        failures = state.redelivery_failures + 1
        at = max(now, state.last_redelivery_failed_at)
        return replace(state, redelivery_failures=failures,
                       first_redelivery_failed_at=at if state.redelivery_failures == 0 else state.first_redelivery_failed_at,
                       last_redelivery_failed_at=at, next_attempt_at=at + redelivery_backoff_seconds(failures))
    raise ValueError(f"unknown wake attempt verdict kind: {verdict.kind!r}")


# LLM: 同因比较只看 reason_code；换了原因从 1 重新计，总次数照加；首次失败时间只在第一次失败时写入。
#   计数失败说明这条唤醒不再处于"连续不计数"，不计数段与它的提醒记录一起清零；batch_failures 保留，
#   直到成功为止都继续逐条单独执行。时钟回拨时按 max(now, last_failed_at) 记，首次失败时间不会晚于最近一次。
# 函数用途: 计算一次计数失败之后的状态。
def _after_failure(state: WakePoisonState, reason_code: str, now: float) -> WakePoisonState:
    same = state.same_cause_count + 1 if reason_code == state.reason_code else 1
    total = state.total_count + 1
    at = max(now, state.last_failed_at)
    return replace(state, reason_code=reason_code, same_cause_count=same, total_count=total,
                   first_failed_at=at if state.total_count == 0 else state.first_failed_at, last_failed_at=at,
                   uncounted_reason_code="", uncounted_count=0, uncounted_since=0.0, stall_alerts=0,
                   last_stall_alert_at=0.0, next_attempt_at=at + poison_backoff_seconds(total))


# LLM: 不计数与批次失败共用：段内次数加 1，原因码取最近一次（空原因的不计数结果保留原码），段起点只在段开始时写入。
# 函数用途: 推进连续不计数段。
def _after_uncounted(state: WakePoisonState, reason_code: str, now: float) -> WakePoisonState:
    return replace(state, uncounted_reason_code=reason_code or state.uncounted_reason_code,
                   uncounted_count=state.uncounted_count + 1,
                   uncounted_since=now if state.uncounted_count == 0 else state.uncounted_since)


# LLM: 判定顺序固定：同因达上限 → 总次数达上限（标 mixed_causes）→ 只重投失败跨度满 24 小时（>=）；都不满足返回 None。
#   批次失败与不计数结果永远不会触发结案。
# 函数用途: 判断当前状态是否应当结案。
def quarantine_decision(state: WakePoisonState) -> QuarantineDecision | None:
    if state.same_cause_count >= WAKE_POISON_SAME_CAUSE_LIMIT_COUNT:
        return _decision(state, state.reason_code, mixed_causes=False)
    if state.total_count >= WAKE_POISON_TOTAL_LIMIT_COUNT:
        return _decision(state, state.reason_code, mixed_causes=True)
    if state.redelivery_failures and (
            state.last_redelivery_failed_at - state.first_redelivery_failed_at >= WAKE_REDELIVERY_GIVE_UP_SECONDS):
        return _decision(state, WAKE_REASON_CHANNEL_UNAVAILABLE, mixed_causes=False)
    return None


# LLM: 连续不计数段满窗口（>=）且这一段还没提醒过，或距上次提醒又满一个窗口时返回提醒；否则返回 None。
#   只判定不写状态，调用方发出提醒后必须用 mark_stall_alerted 记账，否则会重复提醒。
# 函数用途: 判断一条唤醒是否该发"长时间不计数"的运维提醒。
def stall_alert(state: WakePoisonState, *, now: float) -> WakeStallAlert | None:
    if state.uncounted_count == 0 or now - state.uncounted_since < WAKE_UNCOUNTED_STALL_SECONDS:
        return None
    if state.stall_alerts and now - state.last_stall_alert_at < WAKE_UNCOUNTED_STALL_SECONDS:
        return None
    return WakeStallAlert(reason_code=state.uncounted_reason_code, uncounted_count=state.uncounted_count,
                          uncounted_since=state.uncounted_since, alert_number=state.stall_alerts + 1)


# LLM: 与 stall_alert 成对使用；只动提醒计数与时间，不改变不计数段本身。时钟回拨时提醒时间取
#   max(now, 上次提醒, 段起点)，保证"提醒不早于段起点"的一致性。
# 函数用途: 记下已经为当前不计数段发过一次提醒。
def mark_stall_alerted(state: WakePoisonState, *, now: float) -> WakePoisonState:
    return replace(state, stall_alerts=state.stall_alerts + 1,
                   last_stall_alert_at=max(now, state.last_stall_alert_at, state.uncounted_since))


# LLM: 批次失败后直到成功为止都逐条单独执行；调度层据此拆批。
# 函数用途: 判断这条唤醒下一次是否必须单独执行。
def needs_isolation(state: WakePoisonState) -> bool:
    return state.batch_failures > 0


# LLM: n 是计数失败的总次数（不是同因次数），原因交替时退避照样增长；常量改动要同步设计文档第 5 节。
# 函数用途: 计数失败 n 次后的退避秒数：30、60、120、240，之后封顶 300。
def poison_backoff_seconds(failures: int) -> float:
    return _exponential(failures, WAKE_POISON_BACKOFF_BASE_SECONDS, WAKE_POISON_BACKOFF_MAX_SECONDS)


# LLM: n 是只重投失败次数，与计数失败各自独立；封顶 15 分钟由 3a 裁定，改动要同步设计文档第 5 节。
# 函数用途: 只重投失败 n 次后的退避秒数：从 30 秒翻倍，封顶 900 秒。
def redelivery_backoff_seconds(failures: int) -> float:
    return _exponential(failures, WAKE_REDELIVERY_BACKOFF_BASE_SECONDS, WAKE_REDELIVERY_BACKOFF_MAX_SECONDS)


# LLM: 纯计算；指数上限钳到 32 防止大 n 溢出成天文数字，结果再按 cap 封顶。
# 函数用途: 通用封顶指数退避；n 小于 1 时不等待。
def _exponential(failures: int, base: float, cap: float) -> float:
    if failures < 1:
        return 0.0
    return min(base * (2 ** min(failures - 1, 32)), cap)


# LLM: 只拷贝结构化计数，不带时间或错误消息；reason_code 由调用方按判定顺序决定。
# 函数用途: 由状态和结构化原因组装结案判定。
def _decision(state: WakePoisonState, reason_code: str, *, mixed_causes: bool) -> QuarantineDecision:
    return QuarantineDecision(reason_code=reason_code, same_cause_count=state.same_cause_count,
                              total_count=state.total_count, redelivery_failures=state.redelivery_failures,
                              mixed_causes=mixed_causes)


# LLM: 尝试账读不出（DataCorruptionError）时的结案判定：原因 attempt:ledger_corrupt，计数全为 0（账已不可信，不冒充次数），
#   mixed_causes=False。坏账由存储层原样移到 quarantine/ledger/ 留档，不清零重来（清零会让毒丸重新获得无限次机会）。
# 函数用途: 给"尝试账坏了"这条唤醒一个结案判定。
def ledger_corrupt_decision() -> QuarantineDecision:
    return QuarantineDecision(reason_code=WAKE_REASON_LEDGER_CORRUPT, same_cause_count=0, total_count=0,
                              redelivery_failures=0, mixed_causes=False)


# LLM: 优先用结构化 error_code 属性（须通过 is_structured_error_code，空白、小写或带明细的都不算），否则用
#   runtime_error_report 的 category 加异常类名；有结构化 HTTP 状态（provider_error_http_status）时再追加
#   :http_<状态>，让 400 与 413 这类不同的请求问题分成不同原因。绝不读异常消息。
# 函数用途: 为一个异常生成结构化原因码。
def _error_reason_code(error: Exception) -> str:
    from ..backends.errors import provider_error_http_status
    from ..runtime_errors import runtime_error_report

    code = getattr(error, "error_code", "")
    if isinstance(code, str) and is_structured_error_code(code.strip()):
        reason = f"error:{code.strip()}"
    else:
        reason = f"error:{runtime_error_report(error).get('category') or 'unknown'}:{type(error).__name__}"
    status = provider_error_http_status(error)
    return f"{reason}:http_{status}" if status is not None else reason


# LLM: 不计数 = 瞬时类或环境级故障，只按类型或结构化状态码判定：
#   瞬时——供应瞬时（含限流）、供应超时、额度耗尽（compact_guard.is_provider_quota_failure，含压缩调用撞额度的包装）、
#   模型配置暂缺、运行库冲突全族（含锁冲突与待恢复）、
#   取消与压缩让出、本地 IO 的阻塞与超时、SQLite 忙或锁；
#   环境级——统一由 backends.errors.is_provider_environment_fault 判定（与 Gateway 车道暂停同一个权威）：HTTP
#   401/402/403/404/407，以及 ProviderConfigurationError 基类（含 ProviderConnectionError），但请求被拒
#   （ProviderRequestRejectedError）且不是这几个状态时属于请求本身的问题，照样计数。
#   新增类型要在这里显式加，不能靠消息匹配。
# 函数用途: 判断一个异常是否属于不计数的瞬时类或环境级故障。
def _is_uncounted(error: Exception) -> bool:
    from ..backends.errors import is_provider_environment_fault

    return is_provider_environment_fault(error) or _is_transient(error)


# LLM: 瞬时类型清单；与 _is_uncounted 的环境级判定分开，便于逐项测试。
# 函数用途: 判断一个异常是否属于不计数的瞬时类。
def _is_transient(error: Exception) -> bool:
    from ..backends.errors import ProviderTimeoutError, is_provider_transient_error
    from ..runtime_db.operations import RuntimeConflictError
    from ..settings.thread_model_selection import is_model_configuration_unavailable
    from .background_execution import BackgroundCompactSliceYield
    from .compact_guard import is_provider_quota_failure

    if isinstance(error, (InterruptedError, BlockingIOError, TimeoutError, ProviderTimeoutError,
                          RuntimeConflictError, BackgroundCompactSliceYield)):
        return True
    if is_provider_transient_error(error) or is_provider_quota_failure(error):
        return True
    if is_model_configuration_unavailable(error):
        return True
    return isinstance(error, sqlite3.OperationalError) and _sqlite_lock_code(error)


# LLM: 只读整数 sqlite_errorcode 的低 8 位（主结果码），不读消息；属性缺失时按计数处理（宁可停下，不无限重试）。
# 函数用途: 读 sqlite3 异常的结构化结果码（Python 3.11 起才有）；没有这个属性时返回 False，按计数处理。
def _sqlite_lock_code(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return type(code) is int and (code & 0xFF) in _SQLITE_LOCK_PRIMARY_CODES


# LLM: 以字段默认值的类型为准：str 字段要字符串，int 字段要非负 int（bool 不算），其余为有限非负数（拦 NaN/inf）。
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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"wake poison state field {name} must be a finite non-negative number")
    return float(value)


# LLM: 与读回同一口径（to_dict → from_dict，含字段类型与 _check_consistency）；存储层写账前调用，
#   保证写下去的状态一定读得回来，程序缺陷抛 ValueError 而不是留下一份坏账。
# 函数用途: 校验一份即将写盘的状态，不合法抛 ValueError。
def ensure_writable_state(state: WakePoisonState) -> WakePoisonState:
    return WakePoisonState.from_dict(state.to_dict())


# LLM: 计数与时间必须互相吻合：同因不超过总数，原因码与同因次数同时为空或同时非空，
#   没有失败时失败时间为 0、有失败时首次不晚于最近一次；只重投字段同理；没有不计数段时它的原因码、起点和提醒
#   记录都为空；有提醒时最近一次提醒不早于段起点。任何矛盾都按数据损坏处理。
# 函数用途: 校验一份状态内部是否自洽，不自洽抛 ValueError。
def _check_consistency(state: WakePoisonState) -> None:
    problems = [
        state.same_cause_count > state.total_count,
        (state.reason_code == "") != (state.same_cause_count == 0),
        _window_broken(state.total_count, state.first_failed_at, state.last_failed_at),
        _window_broken(state.redelivery_failures, state.first_redelivery_failed_at, state.last_redelivery_failed_at),
        _uncounted_broken(state),
    ]
    if any(problems):
        raise ValueError("wake poison state counts and timestamps contradict each other")


# LLM: 次数为 0 时两端时间都必须是 0；次数大于 0 时首次不能晚于最近一次。
# 函数用途: 判断一组"次数 + 首末时间"是否矛盾。
def _window_broken(count: int, first: float, last: float) -> bool:
    return (first, last) != (0.0, 0.0) if count == 0 else first > last


# LLM: 不计数段为空时原因码、起点、提醒次数与时间都必须为空；提醒次数为 0 时提醒时间为 0，否则提醒不早于段起点。
# 函数用途: 判断连续不计数段与提醒记录是否矛盾。
def _uncounted_broken(state: WakePoisonState) -> bool:
    if state.uncounted_count == 0:
        return (state.uncounted_reason_code, state.uncounted_since, state.stall_alerts, state.last_stall_alert_at) != (
            "", 0.0, 0, 0.0)
    if state.stall_alerts == 0:
        return state.last_stall_alert_at != 0.0
    return state.last_stall_alert_at < state.uncounted_since


__all__ = [
    "WAKE_ATTEMPT_ABANDONED",
    "WAKE_ATTEMPT_GATEWAY_STOPPED",
    "WAKE_ATTEMPT_NEUTRAL",
    "WAKE_ATTEMPT_PATHS",
    "WAKE_ATTEMPT_PATH_CLAIMED",
    "WAKE_ATTEMPT_PATH_QUOTA_FALLBACK",
    "WAKE_ATTEMPT_PATH_REDELIVERY",
    "WAKE_ATTEMPT_SUCCESS",
    "WAKE_CLAIM_CANCELLED",
    "WAKE_CLAIM_FINISHED",
    "WAKE_CLAIM_NOT_EXECUTED",
    "WAKE_CLAIM_STATUSES",
    "WAKE_CLAIM_YIELDED",
    "WAKE_EXPECTED_WAIT_ADMISSIONS",
    "WAKE_POISON_BACKOFF_BASE_SECONDS",
    "WAKE_POISON_BACKOFF_MAX_SECONDS",
    "WAKE_POISON_SAME_CAUSE_LIMIT_COUNT",
    "WAKE_POISON_TOTAL_LIMIT_COUNT",
    "WAKE_REASON_ATTEMPT_ABANDONED",
    "WAKE_REASON_CHANNEL_UNAVAILABLE",
    "WAKE_REASON_COMPACT_YIELD",
    "WAKE_REASON_DELIVERY_NOT_COMMITTED",
    "WAKE_REASON_GATEWAY_STOPPED",
    "WAKE_REASON_LEDGER_CORRUPT",
    "WAKE_REASON_QUOTA_FALLBACK",
    "WAKE_REASON_RUN_CANCELLED",
    "WAKE_REASON_RUN_NO_REPORT",
    "WAKE_REASON_WAKE_NOT_SETTLED",
    "WAKE_REDELIVERY_BACKOFF_BASE_SECONDS",
    "WAKE_REDELIVERY_BACKOFF_MAX_SECONDS",
    "WAKE_REDELIVERY_GIVE_UP_SECONDS",
    "WAKE_STATUS_FAILED_PERMANENTLY",
    "WAKE_UNCOUNTED_STALL_SECONDS",
    "WAKE_VERDICT_BATCH_FAILURE",
    "WAKE_VERDICT_FAILURE",
    "WAKE_VERDICT_NEUTRAL",
    "WAKE_VERDICT_REDELIVERY_FAILURE",
    "WAKE_VERDICT_SUCCESS",
    "QuarantineDecision",
    "WakeAttemptFacts",
    "WakeAttemptVerdict",
    "WakePoisonState",
    "WakeStallAlert",
    "ensure_writable_state",
    "ledger_corrupt_decision",
    "mark_stall_alerted",
    "needs_isolation",
    "next_poison_state",
    "poison_backoff_seconds",
    "quarantine_decision",
    "redelivery_backoff_seconds",
    "stall_alert",
    "verdict_for_admission",
    "verdict_for_attempt",
    "verdict_for_batch",
    "verdict_for_error",
    "verdict_for_missing_report",
    "verdict_for_report",
]
