from __future__ import annotations

# LLM: TaskRuntime composes CardStore and MessageTool into durable task lifecycle operations.
# 模块用途: 创建任务、记录通知路线、选择执行层级、完成任务和恢复过期任务。
from ..cards import (
    CardStore,
    NotificationRouteCard,
    ProgressPolicyCard,
    SessionCard,
    SubagentRunCard,
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

    # LLM: spawn_subagent_session creates an OpenClaw-style child session backed by cards and inbox messages.
    # 函数用途: 创建子代理 session、子任务、运行记录和初始任务消息，并按结构化层级限制派生。
    def spawn_subagent_session(
        self,
        *,
        requester_session_id: str,
        parent_task_id: str,
        goal: str,
        label: str = "",
        mode: str = "run",
        context_mode: str = "isolated",
        cleanup: str = "keep",
        max_spawn_depth: int = 2,
    ) -> SubagentRunCard:
        parent_session = self.cards.get_session(requester_session_id)
        parent_task = self.cards.get_task(parent_task_id)
        parent_depth = int(parent_session.metadata.get("spawn_depth") or 0)
        max_depth = max(1, int(max_spawn_depth))
        if parent_depth >= max_depth:
            raise ValueError("subagent spawn depth exceeded")
        child_depth = parent_depth + 1
        child_role = "leaf" if child_depth >= max_depth else "orchestrator"
        child_control_scope = "none" if child_role == "leaf" else "children"
        child_session_id = new_card_id("subagent_session")
        child_session = SessionCard(
            session_id=child_session_id,
            user_id=parent_session.user_id,
            channel="subagent",
            metadata={
                "parent_session_id": requester_session_id,
                "parent_task_id": parent_task_id,
                "label": label,
                "mode": mode,
                "context_mode": context_mode,
                "spawn_depth": child_depth,
                "subagent_role": child_role,
                "subagent_control_scope": child_control_scope,
                "max_spawn_depth": max_depth,
            },
        )
        self.cards.save_session(child_session)
        child_task = self.cards.create_task(
            goal=goal,
            user_id=parent_task.user_id,
            session_id=child_session_id,
            acceptance=parent_task.acceptance,
            parent_task_id=parent_task_id,
            metadata={
                "worker_tier": "weak_subagent",
                "subagent_label": label,
                "parent_session_id": requester_session_id,
                "context_mode": context_mode,
            },
        )
        self.cards.attach_task_to_session(child_session_id, child_task.task_id)
        run = self.cards.create_subagent_run(
            requester_session_id=requester_session_id,
            child_session_id=child_session_id,
            task_id=child_task.task_id,
            goal=goal,
            controller_session_id=requester_session_id,
            label=label,
            mode=mode,
            context_mode=context_mode,
            cleanup=cleanup,
            expects_completion_message=True,
            metadata={
                "parent_task_id": parent_task_id,
                "user_id": parent_task.user_id,
                "spawn_depth": child_depth,
                "subagent_role": child_role,
                "subagent_control_scope": child_control_scope,
                "max_spawn_depth": max_depth,
            },
        )
        self.messages.send_message(
            sender=MessageTarget(kind="session", identifier=requester_session_id),
            target=MessageTarget(kind="session", identifier=child_session_id),
            content=goal,
            task_id=child_task.task_id,
            message_type="task_assignment",
            idempotency_key=f"{run.run_id}:task_assignment",
            metadata={
                "run_id": run.run_id,
                "parent_task_id": parent_task_id,
                "context_mode": context_mode,
            },
        )
        return run

    # LLM: send_to_subagent routes parent steer/follow-up messages to a child session inbox.
    # 函数用途: 根据 run_id 给子代理 session 发送结构化消息，避免靠共享文本状态追踪。
    def send_to_subagent(
        self,
        *,
        run_id: str,
        message: str,
        message_type: str = "message",
    ):
        run = self.cards.get_subagent_run(run_id)
        return self.messages.send_message(
            sender=MessageTarget(kind="session", identifier=run.requester_session_id),
            target=MessageTarget(kind="session", identifier=run.child_session_id),
            content=message,
            task_id=run.task_id,
            message_type=message_type,
            idempotency_key=f"{run_id}:{message_type}:{message}",
            metadata={"run_id": run_id, "child_session_id": run.child_session_id},
        )

    # LLM: complete_subagent_run records child outcome and delivers the final result back to the parent session.
    # 函数用途: 标记子代理完成、发送最终交付消息并清除 pending_final_delivery。
    def complete_subagent_run(
        self,
        run_id: str,
        *,
        outcome: str,
        artifact_refs: list[str] | None = None,
        summary: str = "",
    ) -> SubagentRunCard:
        completed = self.cards.mark_subagent_completed(
            run_id,
            outcome=outcome,
            artifact_refs=artifact_refs,
            pending_final_delivery=True,
        )
        target = MessageTarget(kind="session", identifier=completed.requester_session_id)
        self.messages.send_message(
            sender=MessageTarget(kind="session", identifier=completed.child_session_id),
            target=target,
            content=summary or f"Subagent completed: {completed.label or completed.child_session_id}",
            task_id=completed.task_id,
            message_type="subagent_completion",
            idempotency_key=f"{run_id}:subagent_completion",
            metadata={
                "run_id": run_id,
                "child_session_id": completed.child_session_id,
                "outcome": outcome,
                "artifact_refs": list(artifact_refs or []),
            },
        )
        return self.cards.mark_subagent_final_delivered(run_id)

    # LLM: kill_subagent_run implements the parent control-plane kill action for child sessions.
    # 函数用途: 终止子代理运行记录，并向子 session 投递 kill 控制消息。
    def kill_subagent_run(self, run_id: str, *, reason: str = "") -> SubagentRunCard:
        killed = self.cards.mark_subagent_killed(run_id, reason=reason)
        self.messages.send_message(
            sender=MessageTarget(kind="session", identifier=killed.requester_session_id),
            target=MessageTarget(kind="session", identifier=killed.child_session_id),
            content=reason or "Subagent run was killed by the controller.",
            task_id=killed.task_id,
            message_type="kill",
            idempotency_key=f"{run_id}:kill",
            metadata={"run_id": run_id, "reason": reason},
        )
        return killed
