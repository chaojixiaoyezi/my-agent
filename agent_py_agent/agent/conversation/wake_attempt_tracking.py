# LLM: 唤醒毒丸第 3 步的接线层：把后台唤醒车道一次领取尝试的结构化事实（路径、领取结果、准入码、报告、异常）收集起来，
#   在尝试结束时逐条唤醒记账（store.wakes.attempts.record），到上限结案（quarantine），并给调度侧提供持久退避、批次隔离与
#   "上一次在途进程已死"的补记（preflight）。判定全部交给 wake_poison 纯函数，持久化全部交给 store_wake_attempts；这里只编排。
#   记账失败绝不能拖垮车道：写盘错误只打结构化日志，尝试账读不出按 ledger_corrupt 结案。进程内在途登记（inflight_attempts）
#   只是给优雅停机找账用的投影，不落盘、不跨进程。改动联测 test_wake_attempt_wiring、test_background_claim_attempt_observer、
#   test_wake_attempt_store，并同步 docs/design/WAKE_POISON_PILL.md 第 10 节。
# 模块用途: 在后台唤醒车道里记下每次领取尝试的结果，让反复同因失败的唤醒按上限结案、不再被无限领取。
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..runtime_errors import DataCorruptionError
from .background_delivery import cached_owner_delivery
from .models import WakeSignal
from .store_wake_attempts import WakeAttemptStart, WakeAttemptStore
from .wake_domain_closeout import (
    close_out_quarantined_wake,
    notify_stalled_wake,
    settle_abandoned_turn,
)
from .wake_poison import (
    WAKE_ATTEMPT_PATH_CLAIMED,
    WAKE_ATTEMPT_PATH_QUOTA_FALLBACK,
    WAKE_CLAIM_FINISHED,
    WAKE_POISON_SAME_CAUSE_LIMIT_COUNT,
    WAKE_REASON_LEDGER_CORRUPT,
    WAKE_VERDICT_FAILURE,
    WAKE_VERDICT_NEUTRAL,
    QuarantineDecision,
    WakeAttemptFacts,
    WakeAttemptVerdict,
    WakeStallAlert,
    ledger_corrupt_decision,
    needs_isolation,
    verdict_for_attempt,
    verdict_for_batch,
)

# begin 拦下这次尝试时交给 run_claimed 的准入码：本次领取不执行，由接线层结案（到上限 / 尝试账读不出）。
WAKE_ATTEMPT_LIMIT_ADMISSION = "wake_attempt_limit"
WAKE_ATTEMPT_LEDGER_CORRUPT_ADMISSION = "wake_attempt_ledger_corrupt"
# 结构化日志前缀，与 [gateway-supply-backoff] 同一格式族：一行 JSON，只有结构化字段，不含正文或错误文案。
_LOG_PREFIX = "[background-wake-poison]"
# 调度器上"会话 → 正在进行的尝试"的进程内映射；同一会话一次只有一条车道在跑。
_TRACKERS_ATTR = "_wake_attempt_trackers"

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[tuple[int, str], InflightWakeAttempt] = {}


# LLM: 只给优雅停机找账用：C6 在关执行池之前对每一项调 attempts.mark_stopping(wake_signal_id, claim_id, now=...)。
#   不是持久事实，进程退出即消失；turn_id 同尝试账 in_flight 里持久化的那一份（进程死后 C6 从账里读，不靠这里）。
# 类用途: 一条正在执行的唤醒领取尝试在进程内的登记。
@dataclass(frozen=True)
class InflightWakeAttempt:
    attempts: WakeAttemptStore
    wake_signal_id: str
    claim_id: str
    turn_id: str = ""


# LLM: 返回快照，调用方遍历期间登记表可以继续变化。只读。
# 函数用途: 列出本进程里所有还在执行的唤醒领取尝试。
def inflight_attempts() -> tuple[InflightWakeAttempt, ...]:
    with _INFLIGHT_LOCK:
        return tuple(_INFLIGHT.values())


# LLM: C6 优雅停机：Gateway 关后台执行池之前调用。对本进程每一条在途尝试写停机标记（mark_stopping 只在 claim 与进程身份
#   都对得上时写），下次 preflight 看到标记按不计数的 attempt:gateway_stopped 补记，部署重启不再给在途唤醒计失败。
#   写失败（OSError 等）逐条吞掉、只打日志，绝不能让停机失败；不等待在途尝试结束，跑完的会照常记账覆盖 in_flight。
# 函数用途: 停机前给本进程所有在途的唤醒尝试打上"正在停机"标记，返回写上标记的条数；会写尝试账。
def mark_inflight_attempts_stopping(*, now: float) -> int:
    marked = 0
    for item in inflight_attempts():
        try:
            marked += bool(item.attempts.mark_stopping(item.wake_signal_id, item.claim_id, now=now))
        except Exception as exc:  # noqa: BLE001 - 停机路径不能因为记账失败而失败
            _log_stopping_failure(item, exc)
    return marked


# 函数用途: 停机标记写失败时打一行结构化日志（只有唤醒 ID 与异常类型）。
def _log_stopping_failure(item: InflightWakeAttempt, exc: BaseException) -> None:
    payload = {"event": "wake_attempt_ledger_unwritable", "stage": "mark_stopping",
               "wake_signal_id": item.wake_signal_id, "error_type": type(exc).__name__}
    try:
        print(f"{_LOG_PREFIX} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}", flush=True)
    except (OSError, ValueError, TypeError):
        pass


# 函数用途: 登记一条开始执行的尝试（begin 成功后）。
def _register_inflight(attempts: WakeAttemptStore, wake_signal_id: str, start: WakeAttemptStart) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT[(id(attempts), wake_signal_id)] = InflightWakeAttempt(
            attempts, wake_signal_id, start.claim_id, start.turn_id,
        )


# 函数用途: 注销一条已经记完账的尝试。
def _unregister_inflight(attempts: WakeAttemptStore, wake_signal_id: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop((id(attempts), wake_signal_id), None)


# LLM: 一批唤醒一次尝试：领取观察者（begin/settled，由 background_claim 回调）与执行路径（note_*）各自填入结构化事实，
#   finish 在尝试结束时逐条记账。不抛异常：记账问题只打日志或按 ledger_corrupt 结案。
# 类用途: 收集并记下一次后台唤醒领取尝试的结果。
class WakeAttemptTracker:
    # LLM: now 是调度器这一拍的时间基准（Gateway 传 time.time()，测试可传合成时间）；记账时刻 = now + 单调时钟经过的时长，
    #   与供应退避按失败时刻起算同一做法，跳过阶段用同一个时间基准比较，长尝试结束后的退避也从结束时刻算。
    #   turn_id 是这一片的精确回合号（接线层按将要执行的唤醒信封算，与 _run_params 同源），begin 时写进每条成员的 in_flight。
    # 函数用途: 绑定调度器、这次一起执行的唤醒批次、时间基准与回合号，不读写文件。
    def __init__(self, scheduler: Any, members: tuple[WakeSignal, ...], now: float, turn_id: str = "") -> None:
        self._scheduler = scheduler
        self.members = tuple(members)
        self._now = float(now)
        self.turn_id = str(turn_id or "")
        self._started = time.monotonic()
        self.path = WAKE_ATTEMPT_PATH_CLAIMED
        self.claim_status = WAKE_CLAIM_FINISHED
        self.admission = ""
        self.report: object = None
        self.error: BaseException | None = None
        self.claim_id = ""
        self.block_code = ""
        self._begun: set[str] = set()
        self._blocked: dict[str, QuarantineDecision] = {}

    # LLM: background_claim.BackgroundClaimAttempt 接口。每条成员写 in_flight 并登记在途；begin 补记出中途死亡后到了上限，
    #   或尝试账读不出，就拦下这次尝试（返回准入码），这些成员在 finish 里结案。写盘失败 fail open：打日志、照常执行。
    # 函数用途: 领到会话 claim 时开始这次尝试，返回非空码表示这次不执行。
    def begin(self, claim_id: str) -> str:
        self.claim_id = str(claim_id or "")
        attempts = _attempts(self._scheduler)
        start = WakeAttemptStart(self.claim_id, batch_size=len(self.members), turn_id=self.turn_id)
        for member in self.members:
            self._begin_member(attempts, member, start)
        if self._blocked:
            corrupt = any(item.reason_code == WAKE_REASON_LEDGER_CORRUPT for item in self._blocked.values())
            self.block_code = WAKE_ATTEMPT_LEDGER_CORRUPT_ADMISSION if corrupt else WAKE_ATTEMPT_LIMIT_ADMISSION
        return self.block_code

    # 函数用途: 为一条成员写 in_flight 并登记在途；读不出账或补记后到上限时记下待结案。
    def _begin_member(self, attempts: WakeAttemptStore, member: WakeSignal, start: WakeAttemptStart) -> None:
        try:
            outcome = attempts.begin(member, start, now=self._clock())
        except DataCorruptionError:
            self._blocked[member.wake_signal_id] = ledger_corrupt_decision()
            return
        except OSError as exc:
            _log("wake_attempt_ledger_unwritable", member, {"stage": "begin", "error_type": type(exc).__name__})
            return
        self._begun.add(member.wake_signal_id)
        _register_inflight(attempts, member.wake_signal_id, start)
        if outcome.abandoned:
            # C6：begin 兜住 preflight 与 begin 之间的竞态；这时新的一片还没执行、没认领任何补充消息。
            settle_abandoned_turn(self._scheduler.runtime.agent, member, outcome.abandoned_turn_id)
        if outcome.decision is not None:
            self._blocked[member.wake_signal_id] = outcome.decision

    # 函数用途: background_claim 回调：这一片的结束方式与领取后的准入码。
    def settled(self, status: str, admission: str) -> None:
        self.claim_status = str(status or WAKE_CLAIM_FINISHED)
        self.admission = str(admission or "")

    # 函数用途: 按调度器的时间基准给出"现在"。
    def _clock(self) -> float:
        return self._now + max(0.0, time.monotonic() - self._started)

    # 函数用途: 执行路径交回报告和路径（claim / 只重投 / 额度通知）。
    def note_execution(self, report: object, path: str) -> None:
        self.report = report
        self.path = path

    # 函数用途: 执行路径抛出的异常（含随后被供应冷却吸收的瞬时异常）；额度分路另标路径。
    def note_error(self, error: BaseException, *, quota: bool) -> None:
        if self.error is None:
            self.error = error
        if quota:
            self.path = WAKE_ATTEMPT_PATH_QUOTA_FALLBACK

    # LLM: 尝试结束时逐条成员记账，注销在途登记；每条成员独立兜住异常，一条出错不影响其它成员。
    # 函数用途: 记下这次尝试对每条唤醒的结果，到上限的结案。
    def finish(self, error: BaseException | None) -> None:
        if error is not None and self.error is None:
            self.error = error
        attempts = _attempts(self._scheduler)
        now = self._clock()
        for member in self.members:
            try:
                self._finish_member(attempts, member, now)
            except Exception as exc:  # noqa: BLE001 - 记账问题只记日志，不拖垮车道
                _log("wake_attempt_ledger_unwritable", member, {"stage": "record", "error_type": type(exc).__name__})
            finally:
                _unregister_inflight(attempts, member.wake_signal_id)

    # LLM: 顺序固定：begin 时就判定要结案的 → 结案；唤醒已不在 pending（被确认、结案或别处消费）→ 删账；
    #   这次被拦下但本成员已写 in_flight → 记一次不计数清掉 in_flight；没开始尝试也没异常 → 不记；其余按
    #   verdict_for_attempt → verdict_for_batch 记账，计数失败打日志，到上限结案，长时间不计数发提醒。
    # 函数用途: 记下这次尝试对一条唤醒的结果。
    def _finish_member(self, attempts: WakeAttemptStore, member: WakeSignal, now: float) -> None:
        wake_id = member.wake_signal_id
        if wake_id in self._blocked:
            quarantine_wake(self._scheduler, member, self._blocked[wake_id], now=now)
            return
        current = _pending_member(self._scheduler, member)
        if current is None:
            attempts.discard(wake_id)
            return
        if self.block_code:
            if wake_id in self._begun:
                attempts.record(member, WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, f"admission:{self.block_code}"), now=now)
            return
        if wake_id not in self._begun and self.path == WAKE_ATTEMPT_PATH_CLAIMED and self.error is None:
            return
        self._record_member(attempts, member, self._facts(current), now)

    # 函数用途: 由收集到的结构化事实组装判定输入；delivery_frozen 按当前 pending 信封重读。
    def _facts(self, current: WakeSignal) -> WakeAttemptFacts:
        return WakeAttemptFacts(path=self.path, claim_status=self.claim_status, admission=self.admission,
                                report=self.report, error=self.error,
                                delivery_frozen=cached_owner_delivery(current) is not None)

    # LLM: 判定输入不自洽（例如只重投却没有报告）是接线缺陷：打日志、只清 in_flight，不写一条猜出来的判定。
    # 函数用途: 按事实记一次账并处理结案与提醒。
    def _record_member(self, attempts: WakeAttemptStore, member: WakeSignal, facts: WakeAttemptFacts, now: float) -> None:
        try:
            verdict = verdict_for_batch(verdict_for_attempt(facts), len(self.members))
        except ValueError:
            _log("wake_attempt_facts_invalid", member, {"path": facts.path, "claim_status": facts.claim_status})
            verdict = WakeAttemptVerdict(WAKE_VERDICT_NEUTRAL, "attempt:facts_invalid")
        try:
            outcome = attempts.record(member, verdict, now=now, error=self.error)
        except DataCorruptionError:
            quarantine_wake(self._scheduler, member, ledger_corrupt_decision(), now=now)
            return
        if verdict.kind == WAKE_VERDICT_FAILURE:
            _log("wake_attempt_failed", member, {
                "reason_code": verdict.reason_code, "same_cause_count": outcome.state.same_cause_count,
                "limit": WAKE_POISON_SAME_CAUSE_LIMIT_COUNT, "next_attempt_at": outcome.state.next_attempt_at})
        if outcome.stall_alert is not None:
            wake_uncounted_stalled(self._scheduler, member, outcome.stall_alert)
        if outcome.decision is not None:
            quarantine_wake(self._scheduler, member, outcome.decision, now=now)


# LLM: 包住一批唤醒的执行（含执行后的确认与兄弟唤醒结案），退出时（finally 语义，拿得到异常）记账；异常原样抛出。
#   执行期间按会话登记在调度器上，_execute_wake_signal 经 current_wake_attempt 取到它交给 run_claimed 作观察者。
#   turn_id 由接线层按将要执行的唤醒信封算好传入（见 WakeAttemptTracker）。
# 函数用途: 为一批唤醒的一次执行开启尝试记账。
@contextmanager
def track_wake_attempt(
    scheduler: Any, members: tuple[WakeSignal, ...], now: float, *, turn_id: str = "",
) -> Iterator[WakeAttemptTracker]:
    tracker = WakeAttemptTracker(scheduler, members, now, turn_id)
    trackers = _trackers(scheduler)
    thread_id = members[0].thread_id if members else ""
    trackers[thread_id] = tracker
    error: BaseException | None = None
    try:
        yield tracker
    except BaseException as exc:
        error = exc
        raise
    finally:
        trackers.pop(thread_id, None)
        tracker.finish(error)


# 函数用途: 取某会话正在进行的尝试；不在唤醒车道里执行时返回 None（观察、策略车道）。
def current_wake_attempt(scheduler: Any, thread_id: str) -> WakeAttemptTracker | None:
    return _trackers(scheduler).get(str(thread_id or ""))


# LLM: 跳过阶段（工作线程里，可以写）：有尝试账才加锁 preflight——上次在途的进程已死时补记（有停机标记记不计数），
#   并按那一片持久化的回合号收尾它认领过的补充消息（C6，settle_abandoned_turn）；坏账按 ledger_corrupt 结案，
#   已到上限的结案；下次最早可试时间未到则跳过。没有账返回 False，零成本。
# 函数用途: 判断一条唤醒这一拍要不要因为毒丸记账而跳过（顺带处理中途死亡补记与结案）。
def wake_attempt_deferred(scheduler: Any, signal: WakeSignal, now: float) -> bool:
    attempts = _attempts(scheduler)
    if not attempts.has_ledger(signal.wake_signal_id):
        return False
    try:
        outcome = attempts.preflight(signal, now=now)
    except DataCorruptionError:
        quarantine_wake(scheduler, signal, ledger_corrupt_decision(), now=now)
        return True
    except OSError as exc:
        _log("wake_attempt_ledger_unwritable", signal, {"stage": "preflight", "error_type": type(exc).__name__})
        return False
    if outcome.abandoned:
        settle_abandoned_turn(scheduler.runtime.agent, signal, outcome.abandoned_turn_id)
    if outcome.stall_alert is not None:
        wake_uncounted_stalled(scheduler, signal, outcome.stall_alert)
    if outcome.decision is not None:
        quarantine_wake(scheduler, signal, outcome.decision, now=now)
        return True
    return now < outcome.state.next_attempt_at


# LLM: 规划线程用（只读、不取锁、不写盘）：与 wake_attempt_deferred 同一个"下次最早可试时间"判据，读不出账时放行，
#   由工作线程的跳过阶段处理。
# 函数用途: 判断一条唤醒是否还在毒丸退避期内，就绪扫描据此不为它排车道。
def wake_attempt_waiting(scheduler: Any, signal: WakeSignal, now: float) -> bool:
    attempts = _attempts(scheduler)
    if not attempts.has_ledger(signal.wake_signal_id):
        return False
    state, error = attempts.state_report(signal.wake_signal_id)
    return error is None and now < state.next_attempt_at


# LLM: 批次隔离：主唤醒上次批次失败过（batch_failures>0）就只跑它自己；需要隔离的兄弟唤醒不进别人的批。只读。
# 函数用途: 按尝试账把一批唤醒拆成需要单独执行的形状。
def isolate_wake_batch(scheduler: Any, batch: tuple[WakeSignal, ...]) -> tuple[WakeSignal, ...]:
    members = tuple(batch)
    if len(members) <= 1:
        return members
    if _needs_isolation(scheduler, members[0]):
        return members[:1]
    return (members[0], *(member for member in members[1:] if not _needs_isolation(scheduler, member)))


# 函数用途: 读尝试账判断一条唤醒是否必须单独执行；没有账或读不出按不需要。
def _needs_isolation(scheduler: Any, signal: WakeSignal) -> bool:
    attempts = _attempts(scheduler)
    if not attempts.has_ledger(signal.wake_signal_id):
        return False
    state, error = attempts.state_report(signal.wake_signal_id)
    return error is None and needs_isolation(state)


# LLM: 结案只经 store.wakes.attempts.quarantine；读不出的信封没有结案后的信号，用选批时读到的冻结副本继续后续动作（3a 裁定 d）。
#   结案后清掉进程内的重试节流与额度待重投标记，再经 wake_domain_closeout 收领域状态（派活 failed、会话消息 rejected）并留
#   宿主提示；唤醒已不在 pending（别处结掉）时不结案也不收尾。副作用：写结案记录、删 pending 与尝试账、结观察、写领域状态与
#   宿主提示、打日志。
# 函数用途: 把一条反复失败的唤醒结案，不再被领取。
def quarantine_wake(scheduler: Any, signal: WakeSignal, decision: QuarantineDecision, *, now: float) -> None:
    try:
        result = _attempts(scheduler).quarantine(signal.wake_signal_id, decision, now=now)
    except Exception as exc:  # noqa: BLE001 - 结案失败留在队列，下次跳过阶段再判
        _log("wake_quarantine_failed", signal, {"reason_code": decision.reason_code, "error_type": type(exc).__name__})
        return
    getattr(scheduler, "_wake_retry_after", {}).pop(signal.wake_signal_id, None)
    getattr(scheduler, "_quota_fallback_wakes", set()).discard(signal.wake_signal_id)
    settled = result.settled or (signal if result.source_unreadable else None)
    if settled is None:
        return
    _log("wake_quarantined", settled, {
        **decision.to_dict(), "source_unreadable": result.source_unreadable,
        "ledger_preserved": bool(result.ledger_preserved_at)})
    try:
        close_out_quarantined_wake(scheduler.runtime.agent, settled, decision)
    except Exception as exc:  # noqa: BLE001 - 领域收尾失败不影响已落盘的结案，只记日志
        _log("wake_domain_closeout_failed", settled, {"error_type": type(exc).__name__})


# LLM: 连续不计数满提醒窗口：store 已在同一把锁里记下这次提醒，这里只负责对外发出（运维日志 + 会话宿主提示，同原因码替换）。
# 函数用途: 发出"长时间不计数"的运维提醒。
def wake_uncounted_stalled(scheduler: Any, signal: WakeSignal, alert: WakeStallAlert) -> None:
    _log("wake_uncounted_stalled", signal, alert.to_dict())
    try:
        notify_stalled_wake(scheduler.runtime.agent, signal, alert)
    except Exception as exc:  # noqa: BLE001 - 提醒写不进去不影响记账，只记日志
        _log("wake_stall_notice_failed", signal, {"error_type": type(exc).__name__})


# 函数用途: 取调度器会话存储上的尝试账入口。
def _attempts(scheduler: Any) -> WakeAttemptStore:
    return scheduler.store.wakes.attempts


# 函数用途: 取（必要时建立）调度器上的会话 → 尝试映射。
def _trackers(scheduler: Any) -> dict[str, WakeAttemptTracker]:
    trackers = getattr(scheduler, _TRACKERS_ATTR, None)
    if not isinstance(trackers, dict):
        trackers = {}
        setattr(scheduler, _TRACKERS_ATTR, trackers)
    return trackers


# LLM: 重读 pending 队列里的这条唤醒；已不在 pending 返回 None；读不出（坏信封）时按仍在 pending 处理，返回选批时的副本。
# 函数用途: 判断一条唤醒在尝试结束时是否还在待处理队列里。
def _pending_member(scheduler: Any, member: WakeSignal) -> WakeSignal | None:
    try:
        return scheduler.store.wakes.pending_one(member.wake_signal_id)
    except Exception:  # noqa: BLE001 - 读不出不等于已处理；照常记账，到上限走读不出结案
        return member


# LLM: 只输出结构化字段；打印失败吞掉，日志不能影响记账。
# 函数用途: 打一行 [background-wake-poison] 结构化日志。
def _log(event: str, signal: WakeSignal, fields: dict[str, object]) -> None:
    payload = {"event": event, "wake_signal_id": signal.wake_signal_id, "thread_id": signal.thread_id,
               "reason": signal.reason, **fields}
    try:
        print(f"{_LOG_PREFIX} {json.dumps(payload, ensure_ascii=False, sort_keys=True)}", flush=True)
    except (OSError, ValueError, TypeError):
        pass


__all__ = [
    "WAKE_ATTEMPT_LEDGER_CORRUPT_ADMISSION",
    "WAKE_ATTEMPT_LIMIT_ADMISSION",
    "InflightWakeAttempt",
    "WakeAttemptTracker",
    "current_wake_attempt",
    "inflight_attempts",
    "mark_inflight_attempts_stopping",
    "isolate_wake_batch",
    "quarantine_wake",
    "track_wake_attempt",
    "wake_attempt_deferred",
    "wake_attempt_waiting",
    "wake_uncounted_stalled",
]
