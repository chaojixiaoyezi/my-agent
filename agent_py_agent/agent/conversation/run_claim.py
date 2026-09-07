# LLM: Share one durable claim lane; host-bound foreground recovery must retain exact task affinity.
# 模块用途: 前后台共用执行权、心跳与释放流程，不新增队列或模型重试。
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


def claim_heartbeat_interval_seconds(
    *,
    ttl_seconds: int,
    configured_interval_seconds: float | None,
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
                renewed = self.store.renew_background_run_claim(
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
            request.store.claim_background_run,
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


# LLM: Ordinary lanes finish in finally. Gateway's write-ahead recovery binding makes terminal
# commit responsible for releasing pinned lanes, including the crash gap after returning a result.
# 函数用途: 保持心跳直到本轮退出；恢复专属车道由请求终态释放，避免返回结果后被抢先续跑。
@contextmanager
def conversation_run_lane(request: ConversationRunLaneRequest) -> Iterator[dict]:
    """Hold the durable per-thread execution claim for one complete model turn."""
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
            request.store.finish_background_run(
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
]
