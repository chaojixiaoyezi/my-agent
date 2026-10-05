# LLM: Share one durable claim lane; host-bound foreground recovery retains task affinity, and
# claim_lost interrupts the owning thread. SLP-1B carries this epoch beside the original attempt_id.
# 租约操作统一访问 store.claims；线程存在性、心跳和最终释放仍使用同一原文件及调用顺序。
# 模块用途: 前后台共用执行权、epoch 心跳栅栏与释放流程；失权会通知旧回合在安全点停止新动作，车道闸仍负责登记与熔断互斥。
"""One durable execution lane shared by foreground and background turns."""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import partial

from ..concurrency.interrupt import register_interruptible, set_interrupt
from ..runtime_errors import runtime_error_report

_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")
# 进程内登记（键是本会话执行租约文件路径，值是在场的用户回合数）：只供后台 Goal 熔断记账判断“用户消息先处理”，
# 不是执行权、不落盘；进程退出即消失，重启后排队的请求重新等车道时再登记。
_USER_INPUT_TURNS: dict[str, int] = {}
_USER_INPUT_TURNS_LOCK = threading.Lock()


# LLM: threading.Lock 不能被弱引用，包一层才能放进弱值字典；有线程在闸内或在等闸时持有强引用，互斥不会因回收而失效。
# 类用途: 一条车道的“用户回合登记 / 熔断判定落账”闸。
class _LaneGate:
    __slots__ = ("lock", "__weakref__")

    # LLM: 只建锁，不登记；登记进闸表由 _lane_gate 在登记表锁内完成。
    # 函数用途: 建一把新的车道闸。
    def __init__(self) -> None:
        self.lock = threading.Lock()


# 车道闸表（键同 _USER_INPUT_TURNS）：没人持有或等待时自动回收，表只随活跃车道数增长，不随见过的会话数无界增长。
_USER_INPUT_LANE_GATES: weakref.WeakValueDictionary[str, _LaneGate] = weakref.WeakValueDictionary()


# LLM: Callers publish any recoverable task binding before acquiring; this flag carries no authority.
# 类用途: 汇集执行车道参数，允许 Gateway 保留原请求的重启恢复归属。
@dataclass(frozen=True)
class ConversationRunLaneRequest:
    """Inputs for one foreground or background owner of a conversation lane."""

    store: object
    thread_id: str
    claim_task_id: str
    reason: str
    lease_seconds: int
    heartbeat_interval_seconds: float
    interrupt_check: Callable[[], bool]
    retry_seconds: float = 0.05
    runtime_facts: dict[str, object] = field(default_factory=dict)
    recover_same_task_only: bool = False
    acquire_transition: Callable[[str, Callable], dict | None] | None = None
    # 这一回合携带用户消息（网关前台用户回合）：从开始排队到退出车道都登记为“用户回合在场”，见 user_input_turn_on_lane。
    carries_user_input: bool = False


# LLM: 续约间隔默认只由租约 TTL 推导（配置里已没有单独的心跳键，原 background_claim_heartbeat_interval_seconds
#   已并入 background_claim_ttl_seconds）；configured_interval_seconds 只留给构造参数/测试显式覆盖，结果永远小于 TTL。
# 函数用途: 按租约秒数算出执行权心跳续约的间隔。
def claim_heartbeat_interval_seconds(
    *,
    ttl_seconds: int,
    configured_interval_seconds: float | None = None,
) -> float:
    """Normalize an explicit or automatic claim heartbeat interval."""
    ttl = max(1.0, float(ttl_seconds or 1))
    configured = float(configured_interval_seconds or 0.0)
    if configured > 0:
        interval = max(0.05, configured)
    elif ttl >= 90.0:
        interval = max(30.0, ttl / 3.0)
    else:
        interval = max(0.05, ttl / 3.0)
    return min(interval, max(0.05, ttl * 0.8))


def detached_task_claim_scope_id(thread_id: str, task_id: str) -> str:
    """Return one durable execution lane key for detached work inside a thread."""
    selected_thread = str(thread_id or "").strip()
    selected_task = str(task_id or "").strip()
    if not selected_thread or not selected_task:
        return ""
    return f"{selected_thread}.task.{selected_task}"


class ConversationRunClaimHeartbeat(threading.Thread):
    # LLM: A renewal failure is a structured claim_lost fact and interrupts the captured owner
    # thread; the durable claim file remains owned by its current epoch and is never edited here.
    # 类用途: 在回合运行期间续租；失权时通知原执行线程通过既有 interrupt 安全点停止新动作。

    # LLM: Capture claim identity and owner thread before starting the daemon; background legacy
    # callers may omit epoch, while session-lane claims always carry it.
    # 函数用途: 准备一次执行权心跳，并保留需要被通知的原回合线程。
    def __init__(self, config: dict):
        thread_id = str(config.get("thread_id") or "")
        super().__init__(name=f"conversation-claim-heartbeat-{thread_id}", daemon=True)
        self.store = config["store"]
        self.thread_id = thread_id
        self.claim_scope_id = str(config.get("claim_scope_id") or "").strip()
        self.claim_id = str(config.get("claim_id") or "")
        epoch = config.get("claim_epoch")
        self.claim_epoch = epoch if type(epoch) is int and epoch > 0 else None
        claim_record = config.get("claim_record")
        self.claim_record = claim_record if isinstance(claim_record, dict) else None
        self.owner_thread_id: int | None = None
        self._owner_interrupt_registration: AbstractContextManager[None] | None = None
        self.lease_seconds = max(1, int(config.get("lease_seconds") or 1))
        self.interval_seconds = max(0.05, float(config.get("interval_seconds") or 0.05))
        self.stop_event = threading.Event()
        self.claim_lost_event = threading.Event()
        self._claim_lost_lock = threading.Lock()
        self._claim_lost_fact: dict[str, object] | None = None

    # LLM: Register the owner before the worker starts so a loss interrupt is always scoped and
    # cleared on exit, including legacy no-task background turns without an outer named registration.
    # 函数用途: 在启动心跳前给原执行线程登记一个内部中断作用域，防止线程复用继承旧停止旗。
    def start(self) -> None:
        self.owner_thread_id = threading.get_ident()
        registration = register_interruptible(
            f"conversation-claim-heartbeat:{self.claim_id or id(self)}"
        )
        registration.__enter__()
        self._owner_interrupt_registration = registration
        try:
            super().start()
        except BaseException:
            self._owner_interrupt_registration = None
            registration.__exit__(None, None, None)
            raise

    # LLM: Stop is called by the owner thread; wake/join the worker, then close only this internal
    # registration without clearing an outer user-stop registration.
    # 函数用途: 停止续租线程并撤销其内部中断作用域，正常收尾不会生成失权事实。
    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=self.interval_seconds + 5.0)
        registration = self._owner_interrupt_registration
        if registration is not None and threading.get_ident() == self.owner_thread_id:
            self._owner_interrupt_registration = None
            registration.__exit__(None, None, None)

    # LLM: The fact is local to this execution and copied for readers; it is not written into a
    # replacement claim because only the owner store can mutate the canonical epoch record.
    # 函数用途: 读取本回合观察到的结构化失权原因。
    @property
    def claim_lost(self) -> dict[str, object] | None:
        with self._claim_lost_lock:
            return dict(self._claim_lost_fact) if self._claim_lost_fact is not None else None

    # LLM: Publish loss once, then signal the exact owner thread through the existing interrupt
    # registry; error text is diagnostic only and never controls state.
    # 函数用途: 记录失权字段并让原回合的工具/模型安全点看到中断旗。
    def _record_claim_lost(self, reason: str, error: BaseException | None = None) -> None:
        with self._claim_lost_lock:
            if self._claim_lost_fact is not None:
                return
            fact: dict[str, object] = {
                "state": "claim_lost",
                "reason": reason,
                "claim_id": self.claim_id,
            }
            if self.claim_epoch is not None:
                fact["claim_epoch"] = self.claim_epoch
            if error is not None:
                fact["error_type"] = type(error).__name__
            self._claim_lost_fact = fact
            if self.claim_record is not None:
                self.claim_record["claim_lost"] = dict(fact)
        if self.owner_thread_id is not None:
            set_interrupt(True, thread_id=self.owner_thread_id)
        self.claim_lost_event.set()

    # LLM: Any failed or mismatched renewal is fail-closed; legacy callers without epoch retain
    # claim_id protection, while current lane callers verify both claim_id and claim_epoch.
    # 函数用途: 周期续租当前 claim，拒绝异常、拒绝回执和 epoch 不匹配，并通知旧执行者停止。
    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            request = {
                "thread_id": self.thread_id,
                "claim_scope_id": self.claim_scope_id,
                "claim_id": self.claim_id,
                "lease_seconds": self.lease_seconds,
                "now": time.time(),
            }
            if self.claim_epoch is not None:
                request["claim_epoch"] = self.claim_epoch
            try:
                renewed = self.store.claims.renew(request)
            except BaseException as exc:  # noqa: BLE001 - heartbeat failure must stop owner safely
                self._record_claim_lost("renew_exception", exc)
                _LOGGER.warning(
                    "conversation claim heartbeat stopped early thread=%s claim=%s: %s",
                    self.thread_id,
                    self.claim_id,
                    runtime_error_report(exc, context="conversation_claim_heartbeat.renew"),
                )
                return
            if renewed is None:
                self._record_claim_lost("renew_rejected")
                return
            if self.claim_epoch is not None and (
                not isinstance(renewed, dict) or renewed.get("claim_epoch") != self.claim_epoch
            ):
                self._record_claim_lost("epoch_mismatch")
                return


# LLM: Acquisition retains cancellation/cadence; a host transition serializes each attempt with
# terminalization, closing the write-ahead-bind/acquire race without holding a lock while waiting.
# 函数用途: 等待并领取会话执行权，每次领取可与停止原子裁决；等待时不持锁、不调用模型。
def _acquire_conversation_run_claim(request: ConversationRunLaneRequest) -> dict:
    while True:
        if request.interrupt_check():
            raise InterruptedError("conversation turn interrupted while waiting for execution lane")
        acquire = partial(
            request.store.claims.acquire,
            {
                "thread_id": request.thread_id,
                "task_id": request.claim_task_id,
                "reason": request.reason,
                "lease_seconds": request.lease_seconds,
                "recover_same_task_only": request.recover_same_task_only,
            }
        )
        claim = (
            request.acquire_transition("claim_conversation", acquire)
            if request.acquire_transition is not None else acquire()
        )
        if claim is not None:
            return claim
        time.sleep(max(0.05, request.retry_seconds))


# LLM: 车道入口：携带用户消息的回合先登记“用户回合在场”，再排队领取执行权，直到退出车道才撤销（覆盖后台片释放车道
#   之后、熔断记账之前的空档）；其余回合（手动 Compact 等）不登记。执行权语义全在 _held_conversation_run_lane，不因登记改变。
#   改动同步 test_goal_fuse_user_turn_first.py。
# 函数用途: 为一个模型回合取得并持有本会话执行车道，必要时让后台 Goal 熔断知道有用户消息在排队或执行。
@contextmanager
def conversation_run_lane(request: ConversationRunLaneRequest) -> Iterator[dict]:
    """Hold the durable per-thread execution claim for one complete model turn."""
    with _user_input_turn_registered(request), _held_conversation_run_lane(request) as claim:
        yield claim


# LLM: 只读进程内登记，不读写租约文件；键与登记时同一个租约文件路径，所以只认同一 owner 存储里的同一会话。
#   只是一次性快照：要依据它落账（Goal 熔断）必须用 user_input_turn_gate，否则查完到落账之间仍可能有用户回合登记进来。
# 函数用途: 回答这个会话的执行车道上此刻有没有携带用户消息的回合（排队中或执行中）。
def user_input_turn_on_lane(store: object, thread_id: str) -> bool:
    key = _user_input_lane_key(store, thread_id)
    with _USER_INPUT_TURNS_LOCK:
        return bool(key) and key in _USER_INPUT_TURNS


# LLM: C5 判定时点：后台 Goal 熔断在这把车道闸里读“用户回合在场”并完成落账，用户回合登记（+1）也要过同一把闸，
#   所以登记要么在判定之前（这一空片不计入），要么在落账之后（暂停已先发生，之后按 D4 只清计数、不自动恢复）。
#   锁顺序：车道闸 → GoalStore 迁移锁/文件锁；持闸期间不得等执行租约、不得回调登记，撤销不经过闸。
#   算不出键时不持任何锁、按“不在场”给出。只读登记，不写盘。改动同步 test_goal_fuse_user_turn_first.py。
# 函数用途: 持有本会话车道闸并给出“此刻有没有用户回合在场”，供熔断判定与落账在闸内一次做完。
@contextmanager
def user_input_turn_gate(store: object, thread_id: str) -> Iterator[bool]:
    key = _user_input_lane_key(store, thread_id)
    if not key:
        yield False
        return
    gate = _lane_gate(key)
    with gate.lock:
        with _USER_INPUT_TURNS_LOCK:
            present = key in _USER_INPUT_TURNS
        yield present


# LLM: 登记与撤销成对出现在同一个 with 里，异常、中断、取消都会撤销；不携带用户消息或算不出键时什么都不做。
#   登记经车道闸（与熔断判定落账互斥），撤销不经闸：用户回合离开车道后再记的空片照常计数。
# 函数用途: 在携带用户消息的回合排队和执行期间登记“用户回合在场”，退出车道时撤销。
@contextmanager
def _user_input_turn_registered(request: ConversationRunLaneRequest) -> Iterator[None]:
    key = _user_input_lane_key(request.store, request.thread_id) if request.carries_user_input else ""
    _register_user_input_turn(key)
    try:
        yield
    finally:
        _adjust_user_input_turns(key, -1)


# LLM: 登记必须在车道闸内：熔断正在判定或落账时等它做完再登记，不能插进“查不在场”和“落账暂停”之间。
#   只持闸做一次计数，不在闸内等执行租约。空键不登记。
# 函数用途: 经车道闸登记一个在场的用户回合。
def _register_user_input_turn(key: str) -> None:
    if not key:
        return
    gate = _lane_gate(key)
    with gate.lock:
        _adjust_user_input_turns(key, 1)


# LLM: 取闸与建闸在登记表锁内完成，同一车道并发取到的是同一把闸；调用方持有返回值期间它不会被回收。
# 函数用途: 取得（必要时新建）某条车道的闸。
def _lane_gate(key: str) -> _LaneGate:
    with _USER_INPUT_TURNS_LOCK:
        gate = _USER_INPUT_LANE_GATES.get(key)
        if gate is None:
            gate = _LaneGate()
            _USER_INPUT_LANE_GATES[key] = gate
        return gate


# LLM: 键取本会话执行租约文件路径（store.claims.storage.background_claim_path），与租约同一份 owner 存储；
#   线程号不合法或存储不可用时返回空串，调用方按“没有登记”处理。
# 函数用途: 算出“用户回合在场”登记用的键。
def _user_input_lane_key(store: object, thread_id: str) -> str:
    storage = getattr(getattr(store, "claims", None), "storage", None)
    selected = str(thread_id or "").strip()
    if storage is None or not selected:
        return ""
    try:
        return str(storage.background_claim_path(selected))
    except (AttributeError, TypeError, ValueError):
        return ""


# LLM: 计数到 0 就删除键，查询只看键在不在；空键不登记。
# 函数用途: 在锁内增减某个会话在场的用户回合数。
def _adjust_user_input_turns(key: str, delta: int) -> None:
    if not key:
        return
    with _USER_INPUT_TURNS_LOCK:
        count = _USER_INPUT_TURNS.get(key, 0) + delta
        if count > 0:
            _USER_INPUT_TURNS[key] = count
        else:
            _USER_INPUT_TURNS.pop(key, None)


# LLM: Persist ordinary lane terminal state with the exact claim id/epoch; recovery-pinned lanes
# remain owned by Gateway terminal commit and must not be finished here.
# 函数用途: 为普通车道回写本轮终态和失权事实，旧 epoch 写回由 ClaimStore 原子拒绝。
def _finish_conversation_run_claim(
    request: ConversationRunLaneRequest,
    claim: dict,
    outcome: dict[str, object],
) -> None:
    if request.recover_same_task_only:
        return
    runtime_facts = dict(request.runtime_facts)
    claim_lost = outcome.get("claim_lost")
    if isinstance(claim_lost, dict):
        runtime_facts["claim_lost"] = claim_lost
    finish_request = {
        "thread_id": request.thread_id,
        "claim_id": str(claim.get("claim_id") or ""),
        "task_id": request.claim_task_id,
        "status": outcome.get("status"),
        "error": outcome.get("error"),
        "runtime_facts": runtime_facts,
    }
    claim_epoch = claim.get("claim_epoch")
    if claim_epoch is not None:
        finish_request["claim_epoch"] = claim_epoch
    request.store.claims.finish(finish_request)


# LLM: Carry the exact claim_epoch into renew/finish and expose the same mutable claim record to
# the heartbeat; lost ownership interrupts the owner thread and stale terminal writes fail CAS.
# 函数用途: 保持心跳直到本轮退出；失权后标记取消并带上结构化原因。
@contextmanager
def _held_conversation_run_lane(request: ConversationRunLaneRequest) -> Iterator[dict]:
    claim = _acquire_conversation_run_claim(request)
    claim_id = str(claim.get("claim_id") or "")
    claim_epoch = claim.get("claim_epoch")
    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": request.store,
            "thread_id": request.thread_id,
            "claim_id": claim_id,
            "claim_epoch": claim_epoch,
            "claim_record": claim,
            "lease_seconds": request.lease_seconds,
            "interval_seconds": request.heartbeat_interval_seconds,
        }
    )
    heartbeat.start()
    status = "finished"
    error: BaseException | None = None
    try:
        yield claim
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
        error = exc
        raise
    finally:
        heartbeat.stop()
        claim_lost = heartbeat.claim_lost
        if claim_lost is not None and status == "finished":
            status = "cancelled"
        _finish_conversation_run_claim(
            request, claim, {"status": status, "error": error, "claim_lost": claim_lost}
        )


__all__ = [
    "ConversationRunClaimHeartbeat",
    "ConversationRunLaneRequest",
    "claim_heartbeat_interval_seconds",
    "conversation_run_lane",
    "detached_task_claim_scope_id",
    "user_input_turn_gate",
    "user_input_turn_on_lane",
]
