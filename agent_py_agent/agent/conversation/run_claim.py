"""One durable execution lane shared by foreground and background turns."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..runtime_errors import runtime_error_report

_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")


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


class ConversationRunClaimHeartbeat(threading.Thread):
    """Renew the per-thread claim while one model turn owns the lane."""

    def __init__(self, config: dict):
        thread_id = str(config.get("thread_id") or "")
        super().__init__(name=f"conversation-claim-heartbeat-{thread_id}", daemon=True)
        self.store = config["store"]
        self.thread_id = thread_id
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


def _acquire_conversation_run_claim(request: ConversationRunLaneRequest) -> dict:
    while True:
        if request.interrupt_check():
            raise InterruptedError("conversation turn interrupted while waiting for execution lane")
        claim = request.store.claim_background_run(
            {
                "thread_id": request.thread_id,
                "task_id": request.claim_task_id,
                "reason": request.reason,
                "lease_seconds": request.lease_seconds,
            }
        )
        if claim is not None:
            return claim
        time.sleep(max(0.05, request.retry_seconds))


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
]
