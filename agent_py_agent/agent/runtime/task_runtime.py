from __future__ import annotations

# LLM: TaskRuntime composes CardStore and MessageTool into durable task lifecycle operations.
# 模块用途: 创建任务、记录通知路线、选择执行层级、完成任务和恢复过期任务。
from ..cards import (
    CardStore,
    NotificationRouteCard,
    ProgressPolicyCard,
    TaskCard,
    TaskStatus,
    new_card_id,
)
from ..messages import MessageTarget, MessageTool
from .worker_tiers import build_worker_context, choose_worker_tier


 # LLM: TaskRuntime is a thin coordinator; execution still belongs to workers.
 # 类用途: 串接任务 Card、进度策略、通知消息和恢复扫描。
class TaskRuntime:
    # LLM: TaskRuntime.__init__ wires card persistence and internal messaging.
    # 函数用途: 初始化任务运行时依赖。
    def __init__(self, cards: CardStore, messages: MessageTool):
        self.cards = cards
        self.messages = messages

    # LLM: create_task records a task order plus route, policy, and worker-tier metadata.
    # 函数用途: 创建任务并写入通知路线、进度策略和执行层级。
    def create_task(
        self,
        *,
        goal: str,
        user_id: str,
        session_id: str,
        complexity: str = "medium",
        progress_interval_seconds: int | None = None,
        acceptance: list[str] | None = None,
    ) -> TaskCard:
        worker_tier = choose_worker_tier(
            complexity=complexity,
            requires_user_memory=complexity in {"large", "complex"},
            requires_feedback=bool(progress_interval_seconds),
        )
        worker_context = build_worker_context(
            worker_tier,
            task_id="pending",
            goal=goal,
            user_id=user_id,
            session_id=session_id,
        )
        task = self.cards.create_task(
            goal=goal,
            user_id=user_id,
            session_id=session_id,
            acceptance=acceptance,
            metadata={
                "complexity": complexity,
                "worker_tier": worker_tier.value,
                "worker_context": worker_context,
            },
        )
        task.metadata["worker_context"]["task_id"] = task.task_id
        self.cards.save_task(task)
        self.cards.save_notification_route(
            NotificationRouteCard(
                route_id=new_card_id("route"),
                task_id=task.task_id,
                user_id=user_id,
                channel="internal",
                target=f"session:{session_id}",
            )
        )
        mode = "interval" if progress_interval_seconds else "completion_only"
        self.cards.save_progress_policy(
            ProgressPolicyCard(
                policy_id=new_card_id("progress"),
                task_id=task.task_id,
                mode=mode,
                interval_seconds=progress_interval_seconds,
            )
        )
        return task

    # LLM: complete_task finalizes a task and sends an idempotent completion message.
    # 函数用途: 标记任务完成、保存产物引用，并通知原 session。
    def complete_task(self, task_id: str, *, artifact_refs: list[str] | None = None) -> TaskCard:
        task = self.cards.get_task(task_id)
        task.artifact_refs = list(artifact_refs or [])
        self.cards.save_task(task)
        completed = self.cards.update_task_status(task_id, TaskStatus.COMPLETED)
        route = self.cards.get_notification_route(task_id)
        if route is not None:
            target = MessageTarget.parse(route.target)
            self.messages.send_message(
                sender=MessageTarget(kind="task", identifier=task_id),
                target=target,
                content=f"Task completed: {completed.goal}",
                task_id=task_id,
                message_type="completion",
                idempotency_key=f"{task_id}:completion:{route.route_id}",
                metadata={"artifact_refs": completed.artifact_refs, "task_id": task_id},
            )
        return completed

    # LLM: recover_expired_tasks requeues running tasks that no longer have an active lease.
    # 函数用途: 扫描失去有效租约的 running 任务并放回队列。
    def recover_expired_tasks(self) -> list[str]:
        recovered: list[str] = []
        for task in self.cards.list_tasks():
            if task.status != TaskStatus.RUNNING:
                continue
            active_lease = self.cards.get_active_lease("task", task.task_id)
            if active_lease is None:
                task.status = TaskStatus.QUEUED
                self.cards.save_task(task)
                self.cards.append_event("task.recovered", task.task_id, {"from_status": TaskStatus.RUNNING, "to_status": TaskStatus.QUEUED})
                recovered.append(task.task_id)
        return recovered

    # LLM: list_queued_tasks exposes queue snapshots for worker dispatchers.
    # 函数用途: 列出当前 queued 状态任务。
    def list_queued_tasks(self) -> list[TaskCard]:
        return [task for task in self.cards.list_tasks() if task.status == TaskStatus.QUEUED]
