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
_TASK_STATUS_TO_RUN_STATUS = {
    "completed": "done",
    "done": "done",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
    "superseded": "cancelled",
    "taken_over": "cancelled",
    "abandoned": "failed",
    "channel_error": "failed",
    "failed": "failed",
    "timeout": "failed",
}


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
                # P0-2 收口(seq1562): extra 结构化字段(run_id/claim_id/failures/
                # error_type)供 JSON formatter 按字段检索落盘, 不依赖正文解析。
                logger.warning(
                    "调度心跳续租失败(瞬时错误, 不退出)",
                    extra={
                        "run_id": self.claim.run_id,
                        "claim_id": self.claim.claim_id,
                        "failures": consecutive_failures,
                        "error_type": type(exc).__name__,
                    },
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

    # LLM: Every queued scheduler run is also the durable root task for its wake. Keep the
    # run id in both typed wake identity and metadata so background lifecycle reads one authority.
    # 函数用途: 预留到期执行并投递同一任务身份的会话唤醒，供 Gateway 后台实际运行。
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
                signal = self.conversation_store.wakes.raise_signal(
                    {
                        "thread_id": str(run["thread_id"]),
                        # 定时执行本身就是这次后台工作的唯一根任务。只把 run_id
                        # 放在 metadata 会让 BackgroundRunRequest 变成 taskless，
                        # 于是模型刚派完子代理时调度器读不到仍为 active 的任务链接，
                        # 误把第一片模型回复结算成 done。
                        "root_task_id": str(run["run_id"]),
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

    # LLM: The scheduler process claim ends when a model slice yields, but the same-id durable
    # task may remain active while children or continuation events run. Preserve that distinction.
    # 函数用途: 把仍有后续工作的定时执行转成等待态，停止进程租约但不写成完成。
    def park_waiting(
        self,
        claim: SchedulerRunClaim,
        *,
        response: str = "",
        delivery_status: str = "",
        delivery_reason: str = "",
        now: float | None = None,
    ) -> dict[str, object] | None:
        try:
            return self.repository.park_run_waiting(
                claim.run_id,
                claim.claim_id,
                response=response,
                delivery_status=delivery_status,
                delivery_reason=delivery_reason,
                now=now,
            )
        except (SchedulerConflictError, SchedulerNotFoundError):
            return None

    # LLM: Reconciliation reads one exact conversation task link and closes only a matching
    # waiting run. Missing, active, corrupt, or unknown task state stays fail-closed and active.
    # 函数用途: 对账一条等待中的定时执行，在对应持久任务真正终结后才结算历史。
    def reconcile_waiting_run(
        self,
        run_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, object] | None:
        selected = str(run_id or "").strip()
        if not selected:
            return None
        run = self.repository.get_active_run(selected)
        if run is None or str(run.get("status") or "") != "waiting":
            return None
        try:
            link = self.conversation_store.tasks.load(selected)
        except Exception:  # noqa: BLE001 - unreadable task authority must keep the run active
            return None
        task_status = str(getattr(link, "status", "") or "").strip().lower()
        terminal_status = _TASK_STATUS_TO_RUN_STATUS.get(task_status)
        if terminal_status is None:
            return None
        error_code = "" if terminal_status == "done" else f"SCHEDULED_TASK_{task_status.upper()}"
        try:
            return self.repository.finish_waiting_run(
                selected,
                SchedulerRunFinish(
                    status=terminal_status,
                    response=str(run.get("response") or ""),
                    delivery_status=str(run.get("delivery_status") or ""),
                    delivery_reason=str(run.get("delivery_reason") or ""),
                    error_code=error_code,
                    error_message=(
                        "" if terminal_status == "done" else f"scheduled task ended as {task_status}"
                    ),
                    now=now,
                ),
            )
        except (SchedulerConflictError, SchedulerNotFoundError):
            return None

    # LLM: Gateway restart and missed lifecycle notifications converge by scanning only scheduler
    # rows already typed as waiting, then applying the same exact-task reconciliation as live wakes.
    # 函数用途: 批量对账当前 owner 的等待定时执行，保证重启后最终状态仍会收口。
    def reconcile_waiting_runs(self, *, now: float | None = None) -> list[str]:
        runs, _errors = self.repository.waiting_runs()
        settled: list[str] = []
        for run in runs:
            run_id = str(run.get("run_id") or "")
            if self.reconcile_waiting_run(run_id, now=now) is not None:
                settled.append(run_id)
        return settled

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
