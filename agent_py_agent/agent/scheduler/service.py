from __future__ import annotations

"""Bridge durable owner schedules into the existing same-thread wake queue."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from .repository import (
    SchedulerConflictError,
    SchedulerNotFoundError,
    SchedulerRepository,
    SchedulerRunFinish,
)

_SCHEDULER_WAKE_REASON = "scheduled_job_due"
_DEFAULT_CLAIM_SECONDS = 300


@dataclass(frozen=True)
class SchedulerRunClaim:
    run_id: str
    claim_id: str
    run: dict[str, object]


@dataclass(frozen=True)
class SchedulerWakeClaimResult:
    status: str
    claim: SchedulerRunClaim | None = None


class SchedulerRunHeartbeat:
    def __init__(
        self,
        repository: SchedulerRepository,
        claim: SchedulerRunClaim,
        *,
        lease_seconds: int,
    ) -> None:
        self.repository = repository
        self.claim = claim
        self.lease_seconds = max(1, int(lease_seconds or 1))
        self.interval_seconds = max(1.0, min(self.lease_seconds / 3.0, 30.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"scheduler-heartbeat-{self.claim.run_id[-10:]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.interval_seconds + 0.5))

    def _loop(self) -> None:
        # P0-2(HANDOFF 文档线): 瞬时 DB 错误(连接抖动/锁冲突)不再静默退出守护
        # 线程——记录结构化日志并继续循环, 指数退避防紧循环(上限 8×interval);
        # alive=False(租约被回收)仍退出; stop event 在退避 sleep 中也能打断
        # (用 _stop.wait 做退避, 而非裸 sleep)。
        consecutive_failures = 0
        while not self._stop.wait(self.interval_seconds):
            try:
                alive = self.repository.heartbeat_run(
                    self.claim.run_id,
                    self.claim.claim_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                consecutive_failures += 1
                logger.warning(
                    "调度心跳续租失败(瞬时错误, 不退出): run_id=%s claim_id=%s "
                    "failures=%d error=%s",
                    self.claim.run_id,
                    self.claim.claim_id,
                    consecutive_failures,
                    type(exc).__name__,
                )
                backoff = min(
                    self.interval_seconds * (2 ** min(consecutive_failures, 3)),
                    self.interval_seconds * 8.0,
                )
                self._stop.wait(backoff)  # 退避期间 stop 可打断
                continue
            consecutive_failures = 0
            if not alive:
                return


class SchedulerService:
    """Reserve, publish, claim, and close scheduler runs using durable facts only."""

    def __init__(
        self,
        repository: SchedulerRepository,
        *,
        conversation_store: Any,
        skill_snapshot_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.conversation_store = conversation_store
        self.skill_snapshot_provider = skill_snapshot_provider

    def enqueue_ready_runs(self, *, now: float | None = None, limit: int = 32) -> list[str]:
        current = float(time.time() if now is None else now)
        self.repository.reserve_due_runs(now=current, limit=limit)
        wake_ids: list[str] = []
        for run in self.repository.queued_runs(now=current, limit=max(1, limit * 2)):
            skill_error = self._skill_reference_error(run)
            if skill_error:
                self._fail_without_execution(run, skill_error, now=current)
                continue
            try:
                signal = self.conversation_store.raise_wake_signal(
                    {
                        "thread_id": str(run["thread_id"]),
                        "urgency": "normal",
                        "reason": _SCHEDULER_WAKE_REASON,
                        "summary": f"Scheduled job is ready: {run['name']}",
                        "dedupe_key": f"scheduler-run:{run['run_id']}",
                        "metadata": _wake_metadata(run),
                        "now": current,
                    }
                )
                wake_id = str(getattr(signal, "wake_signal_id", "") or "")
                self.repository.attach_wake_signal(str(run["run_id"]), wake_id)
                if wake_id:
                    wake_ids.append(wake_id)
            except (KeyError, SchedulerNotFoundError):
                # The conversation or run was removed concurrently. A missing
                # thread cannot be guessed or recreated by the scheduler.
                self._fail_without_execution(run, "SCHEDULER_THREAD_UNAVAILABLE", now=current)
        return wake_ids

    def claim_wake(
        self,
        signal: Any,
        *,
        lease_seconds: int = _DEFAULT_CLAIM_SECONDS,
        now: float | None = None,
    ) -> SchedulerWakeClaimResult:
        metadata = getattr(signal, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        if str(getattr(signal, "reason", "") or "") != _SCHEDULER_WAKE_REASON:
            return SchedulerWakeClaimResult("not_scheduler")
        run_id = str(metadata.get("scheduler_run_id") or "").strip()
        job_id = str(metadata.get("scheduler_job_id") or "").strip()
        if not run_id or not job_id:
            return SchedulerWakeClaimResult("stale")
        run = self.repository.get_active_run(run_id)
        if run is None or str(run.get("job_id") or "") != job_id:
            return SchedulerWakeClaimResult("stale")
        claimed = self.repository.claim_run(
            run_id,
            lease_seconds=max(1, int(lease_seconds or 1)),
            now=now,
        )
        if claimed is None:
            return SchedulerWakeClaimResult("busy")
        claim = SchedulerRunClaim(
            run_id=run_id,
            claim_id=str(claimed["claim_id"]),
            run=claimed,
        )
        self.repository.mark_run_running(run_id, claim.claim_id, now=now)
        return SchedulerWakeClaimResult("claimed", claim)

    def heartbeat(
        self,
        claim: SchedulerRunClaim,
        *,
        lease_seconds: int = _DEFAULT_CLAIM_SECONDS,
    ) -> SchedulerRunHeartbeat:
        return SchedulerRunHeartbeat(
            self.repository,
            claim,
            lease_seconds=lease_seconds,
        )

    def finish(
        self,
        claim: SchedulerRunClaim,
        *,
        status: str,
        response: str = "",
        delivery_status: str = "",
        delivery_reason: str = "",
        error_code: str = "",
        error_message: str = "",
        now: float | None = None,
    ) -> dict[str, object] | None:
        try:
            return self.repository.finish_run(
                claim.run_id,
                claim.claim_id,
                SchedulerRunFinish(
                    status=status,
                    response=response,
                    delivery_status=delivery_status,
                    delivery_reason=delivery_reason,
                    error_code=error_code,
                    error_message=error_message,
                    now=now,
                ),
            )
        except (SchedulerConflictError, SchedulerNotFoundError):
            return None

    def release(self, claim: SchedulerRunClaim, *, now: float | None = None) -> bool:
        return self.repository.release_run_claim(
            claim.run_id,
            claim.claim_id,
            now=now,
        )

    def runtime_snapshot(self) -> dict[str, object]:
        return self.repository.runtime_snapshot()

    def _skill_reference_error(self, run: dict[str, object]) -> str:
        refs = run.get("skill_refs")
        if not isinstance(refs, list) or not refs:
            return ""
        if self.skill_snapshot_provider is None:
            return "SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE"
        try:
            snapshot = self.skill_snapshot_provider()
        except Exception:
            return "SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE"
        for raw in refs:
            if not isinstance(raw, dict):
                return "SCHEDULER_SKILL_REFERENCE_INVALID"
            stable_id = str(raw.get("stable_id") or "")
            expected_sha = str(raw.get("content_sha256") or "")
            entry = snapshot.resolve(stable_id) if snapshot is not None else None
            if entry is None:
                return "SCHEDULER_SKILL_NOT_AVAILABLE"
            if str(getattr(entry, "content_sha256", "") or "") != expected_sha:
                return "SCHEDULER_SKILL_SNAPSHOT_STALE"
        return ""

    def _fail_without_execution(
        self,
        run: dict[str, object],
        error_code: str,
        *,
        now: float,
    ) -> None:
        claimed = self.repository.claim_run(
            str(run.get("run_id") or ""),
            lease_seconds=30,
            now=now,
        )
        if claimed is None:
            return
        claim_id = str(claimed.get("claim_id") or "")
        self.repository.finish_run(
            str(run["run_id"]),
            claim_id,
            SchedulerRunFinish(
                status="failed",
                error_code=error_code,
                error_message="scheduled run failed before model execution",
                now=now,
            ),
        )


def _wake_metadata(run: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "scheduler_wake.v1",
        "scheduler_job_id": str(run["job_id"]),
        "scheduler_run_id": str(run["run_id"]),
        "scheduler_job_version": int(run["job_version"]),
        "scheduler_trigger": str(run["trigger"]),
        "scheduler_scheduled_for": float(run["scheduled_for"]),
        "scheduler_prompt": str(run["prompt"]),
        "scheduler_skill_refs": list(run.get("skill_refs") or []),
        "scheduler_source_task_id": str(run.get("source_task_id") or ""),
    }


def is_scheduler_wake(signal: Any) -> bool:
    return str(getattr(signal, "reason", "") or "").strip().lower() == _SCHEDULER_WAKE_REASON


__all__ = [
    "SchedulerRunClaim",
    "SchedulerRunHeartbeat",
    "SchedulerService",
    "SchedulerWakeClaimResult",
    "is_scheduler_wake",
]
