# LLM: 定时运行只经原领取、会话任务绑定和完成协议进入后台；身份或版本不可证明时禁止采样，不补建旧任务。
#   cheap_checks只控制原仓库的只读投影，不能跳过当前事件预留、取消、恢复或通知策略。
# 合法冻结交付复用原claim而不重开执行；终态映射由本模块统一提供给领取、等待对账和交付收口。
# 模块用途: 把持久定时任务接入原会话唤醒队列，保持执行领取、TaskLink 与收口在同一条主链路。
from __future__ import annotations

"""Bridge durable owner schedules into the existing same-thread wake queue."""

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from ..conversation.learning_source import LEARNING_SOURCE_KEY, schedule_projection
from .active_run_closeout import settle_stale_waiting, waiting_completion_pending
from .recovery import fail_unstarted, prepare_repository
from .repository import (
    SchedulerConflictError,
    SchedulerNotFoundError,
    SchedulerRepository,
    SchedulerRunFinish,
)

_SCHEDULER_WAKE_REASON = "scheduled_job_due"
# 认领租约默认 300 秒：够一次定时回合正常跑完，超时让其它调度器实例接管。
_DEFAULT_CLAIM_SECONDS = 300
# blocked 对这一次定时执行是终态（需要人来看），job 的后续周期照常派发；结算码为 SCHEDULED_TASK_BLOCKED。
_TASK_STATUS_TO_RUN_STATUS = {
    "completed": "done",
    "done": "done",
    "cancelled": "cancelled",
    "interrupted": "cancelled",
    "superseded": "cancelled",
    "taken_over": "cancelled",
    "abandoned": "failed",
    "blocked": "failed",
    "channel_error": "failed",
    "failed": "failed",
    "timeout": "failed",
}


# LLM: 原任务终态映射只有此查询入口；未知状态返回None，调用方不得猜为done或active，也不新增状态别名。
# 函数用途: 为定时领取、等待对账和后台交付收口提供同一份任务到执行终态映射，无写入副作用。
def scheduler_terminal_status_for_task(task_status: str) -> str | None:
    return _TASK_STATUS_TO_RUN_STATUS.get(task_status)


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


# LLM: 本服务只写原 scheduler/ConversationStore 事实；新执行准入可建原 TaskLink，已有 pins/marker 不得覆盖。
#   便宜检查只透传原仓库读口，不能替换创建人、事件来源、取消恢复或静默通知合同。
# 类用途: 预留、领取和收口定时工作，把准确任务身份交给已有后台执行器，不另建权限或执行循环。
class SchedulerService:
    """Reserve, publish, claim, and close scheduler runs using durable facts only."""

    # LLM: follow_up 是组合根注入的 SchedulerFollowUpPolicy（task_follow_up 查询 + 从配置推出的宽限期），
    #   供 active_run_closeout 判定；未注入时一律保持原来的 waiting 行为（fail closed），不走限期结算。
    # 函数用途: 绑定原账本、会话与取消重试能力；仓库投递校验复用同一实际会话库，不能猜配置覆盖后的目录。
    def __init__(
        self,
        repository: SchedulerRepository,
        *,
        conversation_store: Any,
        skill_snapshot_provider: Callable[[], Any] | None = None,
        follow_up: Any | None = None,
        cancellation_handler: Callable[[dict], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.conversation_store = conversation_store
        repository.conversation_store = conversation_store
        self.skill_snapshot_provider = skill_snapshot_provider
        self.follow_up = follow_up
        self.cancellation_handler = cancellation_handler
        prepare_repository(repository, conversation_store, now=time.time())

    # LLM: queued run仍是唯一durable root，便宜检查仅透传给现行dispatch读取；取消、事件和恢复顺序不变。
    # 函数用途: 先沿持久意图重试准确取消并复核已死亡领取，再预留到期执行；仅新 queued 会唤醒，不重跑已开工的未知结果。
    def enqueue_ready_runs(self, *, now: float | None = None, limit: int = 32, cheap_checks: bool = False) -> list[str]:
        from .dispatch import enqueue_ready_runs
        return enqueue_ready_runs(self, now=now, limit=limit, cheap_checks=cheap_checks)

    # LLM: 领取后回读 pending 并验证 TaskLink；此处仅 claimed，原后台车道准入后才由 execution 写 running。
    # 函数用途: 领取定时工作或欠发回复；绑定异常结算，合法冻结只重投，不把等待执行的时间记成工作开始。
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
        if not self.repository.event_run_is_current(run):
            self._fail_without_execution(run, 'EVENT_SOURCE_RETIRED', now=float(time.time() if now is None else now), status='cancelled')
            return SchedulerWakeClaimResult('stale')
        if (
            str(getattr(signal, "thread_id", "") or "") != str(run["thread_id"])
            or str(getattr(signal, "root_task_id", "") or "") != run_id
        ):
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
        try:
            pending_delivery = self._pending_delivery_for_claim(signal, claim)
        except Exception:
            self.release(claim, now=now)
            raise
        if pending_delivery is None:
            self.release(claim, now=now)
            return SchedulerWakeClaimResult("busy")
        from .policy import queue_expired

        if not pending_delivery and queue_expired(claimed, float(time.time() if now is None else now)):
            from .execution import close_unstarted_task

            close_unstarted_task(self, claimed, now=float(time.time() if now is None else now))
            terminal = self.finish(claim, status="skipped", error_code="SCHEDULER_QUEUE_EXPIRED", now=now)
            return SchedulerWakeClaimResult("stale" if terminal is not None else "busy")
        try:
            task_status = self._prepare_task_link(claim, now=now)
            terminal_status = scheduler_terminal_status_for_task(task_status)
            if task_status != "active" and terminal_status is None:
                raise ValueError("scheduled task link status is unknown")
            self.repository.check_run_claim(run_id, claim.claim_id)
        except Exception as exc:
            terminal = self.finish(
                claim, status="failed", error_code="SCHEDULER_TASK_BINDING_INVALID",
                error_message=f"定时任务绑定无法确认（{type(exc).__name__}）", now=now,
            )
            return SchedulerWakeClaimResult("stale" if terminal is not None else "busy")
        if terminal_status and not pending_delivery:
            terminal = self.finish(
                claim, status=terminal_status,
                error_code="" if terminal_status == "done" else f"SCHEDULED_TASK_{task_status.upper()}",
                error_message="" if terminal_status == "done" else f"scheduled task ended as {task_status}",
                now=now,
            )
            return SchedulerWakeClaimResult("stale" if terminal is not None else "busy")
        return SchedulerWakeClaimResult("claimed", claim)

    # LLM: 只从原wake队列回读精确run/job/thread/task身份，再复用原冻结校验；None表示来源不可确认，不是已交付。
    # 函数用途: 区分需要重投的原回复和普通定时工作，读取异常由领取入口释放claim后原样上抛。
    def _pending_delivery_for_claim(self, signal: Any, claim: SchedulerRunClaim) -> bool | None:
        from ..conversation.background_delivery import cached_owner_delivery

        wake_id = str(getattr(signal, "wake_signal_id", "") or "")
        current = self.conversation_store.wakes.pending_one(wake_id)
        run = claim.run
        if (
            current is None
            or current.wake_signal_id != wake_id
            or current.wake_signal_id != str(run.get("wake_signal_id") or "")
            or current.thread_id != str(run["thread_id"])
            or current.root_task_id != claim.run_id
            or current.reason != _SCHEDULER_WAKE_REASON
        ):
            return None
        metadata = current.metadata if isinstance(current.metadata, dict) else {}
        if (
            str(metadata.get("scheduler_run_id") or "") != claim.run_id
            or str(metadata.get("scheduler_job_id") or "") != str(run["job_id"])
        ):
            return None
        return cached_owner_delivery(current) is not None

    # LLM: 原任务锁内读/建准确 link；started_at 由原 repository 在首次运行时写入，不能从 queued 状态猜首次。
    # 仅从未开始且没有 link 的新 run 可创建；已有记录原样保留，绑定失败由 claim_wake 收口，禁止补全局包或选择资格。
    # 函数用途: 在首轮快照前准备原 TaskStore 并固定同一 run 的宿主 actor，已有记录和旧恢复身份不改写。
    def _prepare_task_link(self, claim: SchedulerRunClaim, *, now: float | None) -> str:
        tasks = self.conversation_store.tasks
        with tasks.transition_guard(claim.run_id):
            link, error = tasks.load_report(claim.run_id)
            if error is not None:
                raise ValueError("scheduled task link is unreadable")
            if link is None:
                if float(claim.run.get("started_at") or 0.0) != 0.0:
                    raise ValueError("previously started scheduled task link is missing")
                from .execution_policy import scheduled_run_actor

                link = tasks.bind({
                    "thread_id": str(claim.run["thread_id"]), "task_id": claim.run_id,
                    "goal": str(claim.run["prompt"]), "status": "active", "now": now,
                }, host_channel_actor=scheduled_run_actor(self, claim.run))
            if link.task_id != claim.run_id or link.thread_id != str(claim.run["thread_id"]):
                raise ValueError("scheduled task link identity differs from the claimed run")
            return str(link.status)

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

    # LLM: 等待对账读取精确 TaskLink、执行 claim 和 pending wake；completed 的最终投递未确认时不得结束 run。
    #   最终回执已在原 run 同事务保存，再确认 wake；重启对账沿同一字段收口，不用第一片的旧回复代替最终回执。
    #   任务仍是 active 时走 active_run_closeout.settle_stale_waiting 这个结构化出口：停满宽限期且没有任何后续工作才结算，否则继续等。
    # 函数用途: 在对应持久任务真正终结后结算原waiting运行，不负责冻结回复重投或另建执行状态。
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
        terminal_status = scheduler_terminal_status_for_task(task_status)
        if terminal_status is None:
            return settle_stale_waiting(self, run, now=now) if task_status == "active" else None
        if run.get('cancel_requested_at'):
            from .active_run_closeout import _task_execution_claim_live_or_unknown
            if not run.get('cancel_cleanup_confirmed') or _task_execution_claim_live_or_unknown(self, run):
                return None
        if terminal_status == "done" and waiting_completion_pending(self, run, now=now):
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
    # LLM: 可选投影只提供waiting行；每条运行仍按准确ID现读原任务并执行同一终态对账。
    # 函数用途: 批量对账当前 owner 的等待定时执行，保证重启后最终状态仍会收口。
    def reconcile_waiting_runs(self, *, now: float | None = None, cheap_checks: bool = False) -> list[str]:
        runs, _errors = self.repository.waiting_runs(cheap_checks=cheap_checks)
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

    # LLM: 真正唤醒前核对原 refs；内容摘要相同但 activation 变化也必须拒绝，不通过重装恢复过期授权。
    # 函数用途: 在模型或后台任务启动前判断所选公开 Skill／能力包是否仍为固定版本。
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
        from ..capability.task_references import normalize_skill_reference

        for raw in refs:
            try:
                raw = normalize_skill_reference(raw)
            except ValueError:
                return "SCHEDULER_SKILL_REFERENCE_INVALID"
            stable_id = str(raw.get("stable_id") or "")
            expected_sha = str(raw.get("content_sha256") or "")
            entry = snapshot.resolve_reference(stable_id) if snapshot is not None else None
            if entry is None:
                return "SCHEDULER_SKILL_NOT_AVAILABLE"
            if str(getattr(entry, "content_sha256", "") or "") != expected_sha:
                return "SCHEDULER_SKILL_SNAPSHOT_STALE"
            if raw.get("kind") == "capability_package":
                try:
                    if normalize_skill_reference(entry.to_ref()) != normalize_skill_reference(raw):
                        return "SCHEDULER_SKILL_SNAPSHOT_STALE"
                except ValueError:
                    return "SCHEDULER_SKILL_REFERENCE_INVALID"
        return ""

    # LLM: 先取原 claim；从未开始的 TaskLink 只按精确归属关闭，不删除或重建记录，终态仍由原仓库 CAS。
    # 函数用途: 结算开始前过期或不可执行的 run，避免排队阶段留下活动任务。
    def _fail_without_execution(
        self,
        run: dict[str, object],
        error_code: str,
        *,
        now: float,
        status: str = "failed",
    ) -> None:
        fail_unstarted(self, run, error_code, now=now, status=status)


# LLM: 来源仅由持久scheduler run构造；定时唤醒不可学习，模型字段不能覆盖，保留main的任务/权限元数据。
# 函数用途: 把定时运行事实投影到当前后台唤醒元数据。
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
        LEARNING_SOURCE_KEY: schedule_projection(),
    }


def is_scheduler_wake(signal: Any) -> bool:
    return str(getattr(signal, "reason", "") or "").strip().lower() == _SCHEDULER_WAKE_REASON


__all__ = [
    "SchedulerRunClaim",
    "SchedulerRunHeartbeat",
    "SchedulerService",
    "SchedulerWakeClaimResult",
    "is_scheduler_wake",
    "scheduler_terminal_status_for_task",
]
