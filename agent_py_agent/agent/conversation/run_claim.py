# LLM: Share one durable claim lane; host-bound foreground recovery must retain exact task affinity.
# 租约操作统一访问 store.claims；线程存在性、心跳和最终释放仍使用同一原文件及调用顺序。
# 模块用途: 前后台共用执行权、心跳与释放流程，不新增队列或模型重试；另在进程内登记“用户回合在场”，供 Goal 熔断记账读取。
"""One durable execution lane shared by foreground and background turns."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial

from ..runtime_errors import runtime_error_report

_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")
# 进程内登记（键是本会话执行租约文件路径，值是在场的用户回合数）：只供后台 Goal 熔断记账判断“用户消息先处理”，
# 不是执行权、不落盘；进程退出即消失，重启后排队的请求重新等车道时再登记。
_USER_INPUT_TURNS: dict[str, int] = {}
_USER_INPUT_TURNS_LOCK = threading.Lock()


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
    """Renew the per-thread claim while one model turn owns the lane."""

    def __init__(self, config: dict):
        thread_id = str(config.get("thread_id") or "")
        super().__init__(name=f"conversation-claim-heartbeat-{thread_id}", daemon=True)
        self.store = config["store"]
        self.thread_id = thread_id
        self.claim_scope_id = str(config.get("claim_scope_id") or "").strip()
        self.claim_id = str(config.get("claim_id") or "")
        self.lease_seconds = max(1, int(config.get("lease_seconds") or 1))
        self.interval_seconds = max(0.05, float(config.get("interval_seconds") or 0.05))
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=self.interval_seconds + 5.0)

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                renewed = self.store.claims.renew(
                    {
                        "thread_id": self.thread_id,
                        "claim_scope_id": self.claim_scope_id,
                        "claim_id": self.claim_id,
                        "lease_seconds": self.lease_seconds,
                        "now": time.time(),
                    }
                )
            except BaseException as exc:  # noqa: BLE001 - daemon heartbeat must not crash raw
                _LOGGER.warning(
                    "conversation claim heartbeat stopped early thread=%s claim=%s: %s",
                    self.thread_id,
                    self.claim_id,
                    runtime_error_report(exc, context="conversation_claim_heartbeat.renew"),
                )
                return
            if renewed is None:
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
#   后台 Goal 熔断记账用它判断“空片结束时本会话是否有用户消息在排队或执行”（C5/O4：用户消息先处理）。
# 函数用途: 回答这个会话的执行车道上此刻有没有携带用户消息的回合（排队中或执行中）。
def user_input_turn_on_lane(store: object, thread_id: str) -> bool:
    key = _user_input_lane_key(store, thread_id)
    with _USER_INPUT_TURNS_LOCK:
        return bool(key) and key in _USER_INPUT_TURNS


# LLM: 登记与撤销成对出现在同一个 with 里，异常、中断、取消都会撤销；不携带用户消息或算不出键时什么都不做。
# 函数用途: 在携带用户消息的回合排队和执行期间登记“用户回合在场”，退出车道时撤销。
@contextmanager
def _user_input_turn_registered(request: ConversationRunLaneRequest) -> Iterator[None]:
    key = _user_input_lane_key(request.store, request.thread_id) if request.carries_user_input else ""
    _adjust_user_input_turns(key, 1)
    try:
        yield
    finally:
        _adjust_user_input_turns(key, -1)


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


# LLM: Ordinary lanes finish in finally. Gateway's write-ahead recovery binding makes terminal
# commit responsible for releasing pinned lanes, including the crash gap after returning a result.
# 函数用途: 保持心跳直到本轮退出；恢复专属车道由请求终态释放，避免返回结果后被抢先续跑。
@contextmanager
def _held_conversation_run_lane(request: ConversationRunLaneRequest) -> Iterator[dict]:
    claim = _acquire_conversation_run_claim(request)
    claim_id = str(claim.get("claim_id") or "")
    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": request.store,
            "thread_id": request.thread_id,
            "claim_id": claim_id,
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
        if not request.recover_same_task_only:
            request.store.claims.finish(
                {
                    "thread_id": request.thread_id,
                    "claim_id": claim_id,
                    "task_id": request.claim_task_id,
                    "status": status,
                    "error": error,
                    "runtime_facts": dict(request.runtime_facts),
                }
            )


__all__ = [
    "ConversationRunClaimHeartbeat",
    "ConversationRunLaneRequest",
    "claim_heartbeat_interval_seconds",
    "conversation_run_lane",
    "detached_task_claim_scope_id",
    "user_input_turn_on_lane",
]
