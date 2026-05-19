from __future__ import annotations

# LLM: CardStore is the local durable fact store for cards; preserve JSON shapes and append-only event semantics.
# 模块用途: 提供 Card 的文件持久化、状态迁移、租约、恢复点、事件和完整性检查。
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from ..io import append_jsonl
from .models import (
    TERMINAL_STATUSES,
    VALID_TASK_TRANSITIONS,
    CheckpointCard,
    EventCard,
    LeaseCard,
    NotificationRouteCard,
    ProgressPolicyCard,
    SessionCard,
    SubagentRunCard,
    SubagentRunStatus,
    TaskCard,
    TaskStatus,
    WorkerRunCard,
    new_card_id,
    now_ts,
)


 # LLM: CardStore owns file-backed card persistence without executing task work.
 # 类用途: 管理 SessionCard、TaskCard、LeaseCard、CheckpointCard 和事件日志的读写。
class CardStore:
    # LLM: CardStore.__init__ defines stable storage folders under a caller-provided root.
    # 函数用途: 初始化 CardStore 的根目录和各类 Card 文件目录。
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.sessions_dir = self.root / "sessions"
        self.leases_dir = self.root / "leases"
        self.worker_runs_dir = self.root / "worker_runs"
        self.subagent_runs_dir = self.root / "subagent_runs"
        self.routes_dir = self.root / "notification_routes"
        self.policies_dir = self.root / "progress_policies"
        self.events_path = self.root / "events.jsonl"

    # LLM: save_session persists a conversation container without invoking agents.
    # 函数用途: 保存 SessionCard，用于恢复会话和活跃任务绑定。
    def save_session(self, session: SessionCard) -> None:
        session.updated_at = now_ts()
        write_json_file_atomic(self.sessions_dir / f"{session.session_id}.json", session.to_dict())

    # LLM: get_session loads a persisted SessionCard by id.
    # 函数用途: 根据 session_id 读取 SessionCard，不存在时抛出 KeyError。
    def get_session(self, session_id: str) -> SessionCard:
        payload = read_json_file(self.sessions_dir / f"{session_id}.json")
        if not payload:
            raise KeyError(session_id)
        return SessionCard.from_dict(payload)

    # LLM: attach_task_to_session records task visibility for a session.
    # 函数用途: 把任务 id 挂到 session 的活跃任务列表，并写入事件。
    def attach_task_to_session(self, session_id: str, task_id: str) -> SessionCard:
        with self.lock(f"session:{session_id}"):
            session = self.get_session(session_id)
            if task_id not in session.active_task_ids:
                session.active_task_ids.append(task_id)
            self.save_session(session)
        self.append_event("session.task_attached", task_id, {"session_id": session_id})
        return session

    # LLM: detach_task_from_session removes terminal tasks from the foreground active list.
    # 函数用途: 后台任务结束后让 session.active_task_ids 不无限增长。
    def detach_task_from_session(self, session_id: str, task_id: str) -> SessionCard:
        with self.lock(f"session:{session_id}"):
            session = self.get_session(session_id)
            session.active_task_ids = [item for item in session.active_task_ids if item != task_id]
            self.save_session(session)
        self.append_event("session.task_detached", task_id, {"session_id": session_id})
        return session

    # LLM: create_task creates a durable task order and emits a task.created event.
    # 函数用途: 创建 TaskCard，可选挂到父任务，并记录创建事件。
    def create_task(
        self,
        *,
        goal: str,
        user_id: str,
        session_id: str,
        acceptance: list[str] | None = None,
        parent_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskCard:
        task = TaskCard(
            task_id=new_card_id("task"),
            goal=goal,
            user_id=user_id,
            session_id=session_id,
            acceptance=list(acceptance or []),
            parent_task_id=parent_task_id,
            metadata=dict(metadata or {}),
        )
        self.save_task(task)
        self.append_event("task.created", task.task_id, {"status": task.status})
        if parent_task_id:
            parent = self.get_task(parent_task_id)
            parent.child_task_ids.append(task.task_id)
            self.save_task(parent)
        return task

    # LLM: save_task writes a TaskCard snapshot atomically.
    # 函数用途: 保存任务当前快照，供队列、恢复和查询读取。
    def save_task(self, task: TaskCard) -> None:
        task.updated_at = now_ts()
        write_json_file_atomic(self._task_path(task.task_id), task.to_dict())

    # LLM: get_task loads a TaskCard by id and fails loudly when absent.
    # 函数用途: 根据 task_id 读取 TaskCard，不存在时抛出 KeyError。
    def get_task(self, task_id: str) -> TaskCard:
        payload = read_json_file(self._task_path(task_id))
        if not payload:
            raise KeyError(task_id)
        return TaskCard.from_dict(payload)

    # LLM: list_tasks returns current task snapshots for queue and supervisor scans.
    # 函数用途: 列出 CardStore 下所有 TaskCard。
    def list_tasks(self) -> list[TaskCard]:
        try:
            paths = sorted(self.tasks_dir.glob("*.json"))
        except OSError:
            return []
        tasks: list[TaskCard] = []
        for path in paths:
            payload = read_json_file(path)
            if payload:
                tasks.append(TaskCard.from_dict(payload))
        return sorted(tasks, key=lambda task: (task.created_at, task.task_id))

    # LLM: update_task_status enforces the state machine and emits a transition event.
    # 函数用途: 按合法状态机更新任务状态，并记录状态变化事件。
    def update_task_status(self, task_id: str, status: TaskStatus | str, *, reason: str = "") -> TaskCard:
        task = self.get_task(task_id)
        next_status = TaskStatus(str(status))
        if next_status not in VALID_TASK_TRANSITIONS[task.status]:
            raise ValueError(f"invalid task status transition: {task.status} -> {next_status}")
        previous = task.status
        task.status = next_status
        self.save_task(task)
        if next_status in TERMINAL_STATUSES and task.session_id:
            try:
                self.detach_task_from_session(task.session_id, task.task_id)
            except KeyError:
                pass
        self.append_event(
            "task.status_changed",
            task.task_id,
            {"from_status": previous, "to_status": next_status, "reason": reason},
        )
        return task

    # LLM: acquire_lease grants a resource only when no active lease exists.
    # 函数用途: 获取任务、文件或 worker slot 的限时租约，防止重复执行。
    def acquire_lease(
        self,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        *,
        task_id: str | None = None,
        ttl_seconds: float = 60,
        metadata: dict[str, Any] | None = None,
    ) -> LeaseCard | None:
        with self.lock(f"lease:{resource_type}:{resource_id}"):
            active = self.get_active_lease(resource_type, resource_id)
            if active is not None:
                return None
            now = now_ts()
            lease = LeaseCard(
                lease_id=new_card_id("lease"),
                resource_type=resource_type,
                resource_id=resource_id,
                owner_id=owner_id,
                task_id=task_id,
                expires_at=now + ttl_seconds,
                metadata=dict(metadata or {}),
            )
            write_json_file_atomic(self._lease_path(lease.lease_id), lease.to_dict())
        if task_id:
            self.append_event("lease.acquired", task_id, {"lease_id": lease.lease_id, "owner_id": owner_id, "resource_type": resource_type})
        return lease

    # LLM: get_active_lease finds the current live lease for a resource.
    # 函数用途: 查询某个资源是否已有未过期且未释放的租约。
    def get_active_lease(self, resource_type: str, resource_id: str) -> LeaseCard | None:
        for lease in self.list_leases():
            if lease.resource_type == resource_type and lease.resource_id == resource_id and lease.is_active:
                return lease
        return None

    # LLM: list_leases returns lease snapshots so worker pools can count capacity.
    # 函数用途: 列出所有租约记录，供并发控制和恢复扫描使用。
    def list_leases(self) -> list[LeaseCard]:
        try:
            paths = sorted(self.leases_dir.glob("*.json"))
        except OSError:
            return []
        leases = []
        for path in paths:
            payload = read_json_file(path)
            if payload:
                leases.append(LeaseCard.from_dict(payload))
        return leases

    # LLM: release_lease marks a lease as released without deleting audit facts.
    # 函数用途: 释放租约，并在有关联任务时写入释放事件。
    def release_lease(self, lease_id: str) -> bool:
        path = self._lease_path(lease_id)
        payload = read_json_file(path)
        if not payload:
            return False
        lease = LeaseCard.from_dict(payload)
        if lease.released_at is not None:
            return True
        lease.released_at = now_ts()
        lease.updated_at = lease.released_at
        write_json_file_atomic(path, lease.to_dict())
        if lease.task_id:
            self.append_event("lease.released", lease.task_id, {"lease_id": lease.lease_id, "owner_id": lease.owner_id})
        return True

    # LLM: save_checkpoint records resumable task progress and emits an event.
    # 函数用途: 保存任务 checkpoint，供崩溃恢复和续跑使用。
    def save_checkpoint(self, checkpoint: CheckpointCard) -> None:
        checkpoint.updated_at = now_ts()
        write_json_file_atomic(self._checkpoint_path(checkpoint.task_id, checkpoint.checkpoint_id), checkpoint.to_dict())
        self.append_event("checkpoint.saved", checkpoint.task_id, {"checkpoint_id": checkpoint.checkpoint_id, "step": checkpoint.step})

    # LLM: latest_checkpoint returns the most recent checkpoint snapshot for a task.
    # 函数用途: 获取任务最新恢复点；没有 checkpoint 时返回 None。
    def latest_checkpoint(self, task_id: str) -> CheckpointCard | None:
        checkpoint_dir = self._checkpoint_dir(task_id)
        try:
            paths = sorted(checkpoint_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
        except OSError:
            return None
        if not paths:
            return None
        return CheckpointCard.from_dict(read_json_file(paths[-1]))

    # LLM: save_notification_route stores the return address for a task.
    # 函数用途: 保存任务完成或进度通知应该发往哪里。
    def save_notification_route(self, route: NotificationRouteCard) -> None:
        route.updated_at = now_ts()
        write_json_file_atomic(self.routes_dir / f"{route.task_id}.json", route.to_dict())

    # LLM: get_notification_route loads the return address for a task.
    # 函数用途: 获取任务通知路线；没有时返回 None。
    def get_notification_route(self, task_id: str) -> NotificationRouteCard | None:
        payload = read_json_file(self.routes_dir / f"{task_id}.json")
        return NotificationRouteCard.from_dict(payload) if payload else None

    # LLM: save_progress_policy stores feedback cadence independently of prompts.
    # 函数用途: 保存任务进度汇报策略。
    def save_progress_policy(self, policy: ProgressPolicyCard) -> None:
        policy.updated_at = now_ts()
        write_json_file_atomic(self.policies_dir / f"{policy.task_id}.json", policy.to_dict())

    # LLM: get_progress_policy loads feedback cadence for dispatchers.
    # 函数用途: 获取任务进度汇报策略；没有时返回 None。
    def get_progress_policy(self, task_id: str) -> ProgressPolicyCard | None:
        payload = read_json_file(self.policies_dir / f"{task_id}.json")
        return ProgressPolicyCard.from_dict(payload) if payload else None

    # LLM: save_worker_run persists one concrete worker execution attempt.
    # 函数用途: 保存 WorkerRunCard，供恢复、调试和多 worker 观测使用。
    def save_worker_run(self, worker_run: WorkerRunCard) -> None:
        worker_run.updated_at = now_ts()
        write_json_file_atomic(self.worker_runs_dir / f"{worker_run.worker_run_id}.json", worker_run.to_dict())

    # LLM: create_worker_run opens an auditable worker attempt card.
    # 函数用途: 为 task/worker/lease 创建 WorkerRunCard 并记录事件。
    def create_worker_run(
        self,
        *,
        task_id: str,
        worker_id: str,
        worker_type: str = "task_agent",
        lease_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkerRunCard:
        worker_run = WorkerRunCard(
            worker_run_id=new_card_id("worker_run"),
            task_id=task_id,
            worker_id=worker_id,
            worker_type=worker_type,
            lease_id=lease_id,
            metadata=dict(metadata or {}),
        )
        self.save_worker_run(worker_run)
        self.append_event("worker_run.created", task_id, {"worker_run_id": worker_run.worker_run_id, "worker_id": worker_id})
        return worker_run

    # LLM: get_worker_run loads a persisted WorkerRunCard by id.
    # 函数用途: 根据 worker_run_id 读取一次具体执行记录。
    def get_worker_run(self, worker_run_id: str) -> WorkerRunCard:
        payload = read_json_file(self.worker_runs_dir / f"{worker_run_id}.json")
        if not payload:
            raise KeyError(worker_run_id)
        return WorkerRunCard.from_dict(payload)

    # LLM: list_worker_runs returns execution attempts for dashboards and recovery checks.
    # 函数用途: 列出当前 store 下的所有 WorkerRunCard。
    def list_worker_runs(self, task_id: str | None = None) -> list[WorkerRunCard]:
        try:
            paths = sorted(self.worker_runs_dir.glob("*.json"))
        except OSError:
            return []
        runs: list[WorkerRunCard] = []
        for path in paths:
            payload = read_json_file(path)
            if not payload:
                continue
            run = WorkerRunCard.from_dict(payload)
            if task_id is None or run.task_id == task_id:
                runs.append(run)
        return runs

    # LLM: create_subagent_run opens the durable run registry row for a child session.
    # 函数用途: 创建子代理运行记录，绑定父 session、子 session、任务和上下文模式。
    def create_subagent_run(
        self,
        *,
        requester_session_id: str,
        child_session_id: str,
        task_id: str,
        goal: str,
        controller_session_id: str = "",
        label: str = "",
        mode: str = "run",
        context_mode: str = "isolated",
        cleanup: str = "keep",
        expects_completion_message: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SubagentRunCard:
        run = SubagentRunCard(
            run_id=new_card_id("subagent_run"),
            requester_session_id=requester_session_id,
            child_session_id=child_session_id,
            controller_session_id=controller_session_id or requester_session_id,
            task_id=task_id,
            goal=goal,
            label=label,
            mode=mode,
            context_mode=context_mode,
            cleanup=cleanup,
            expects_completion_message=expects_completion_message,
            metadata=dict(metadata or {}),
        )
        self.save_subagent_run(run)
        self.append_event(
            "subagent_run.created",
            task_id,
            {
                "run_id": run.run_id,
                "requester_session_id": requester_session_id,
                "child_session_id": child_session_id,
                "controller_session_id": run.controller_session_id,
                "mode": mode,
                "context_mode": context_mode,
            },
        )
        return run

    # LLM: save_subagent_run persists the current subagent run snapshot atomically.
    # 函数用途: 保存子代理运行记录，供恢复、状态查询和控制面读取。
    def save_subagent_run(self, run: SubagentRunCard) -> None:
        run.updated_at = now_ts()
        write_json_file_atomic(self._subagent_run_path(run.run_id), run.to_dict())

    # LLM: get_subagent_run loads one durable child-session run record by id.
    # 函数用途: 根据 run_id 读取子代理运行记录，不存在时抛出 KeyError。
    def get_subagent_run(self, run_id: str) -> SubagentRunCard:
        payload = read_json_file(self._subagent_run_path(run_id))
        if not payload:
            raise KeyError(run_id)
        return SubagentRunCard.from_dict(payload)

    # LLM: list_subagent_runs gives control-plane code filtered views without scanning prompt text.
    # 函数用途: 按父 session、子 session 或 task_id 列出子代理运行记录。
    def list_subagent_runs(
        self,
        *,
        requester_session_id: str | None = None,
        child_session_id: str | None = None,
        task_id: str | None = None,
    ) -> list[SubagentRunCard]:
        try:
            paths = sorted(self.subagent_runs_dir.glob("*.json"))
        except OSError:
            return []
        runs: list[SubagentRunCard] = []
        for path in paths:
            payload = read_json_file(path)
            if not payload:
                continue
            run = SubagentRunCard.from_dict(payload)
            if requester_session_id is not None and run.requester_session_id != requester_session_id:
                continue
            if child_session_id is not None and run.child_session_id != child_session_id:
                continue
            if task_id is not None and run.task_id != task_id:
                continue
            runs.append(run)
        return sorted(runs, key=lambda item: (item.created_at, item.run_id))

    # LLM: mark_subagent_started records the transition from created to running once.
    # 函数用途: 标记子代理运行开始，并写入 started 事件。
    def mark_subagent_started(self, run_id: str) -> SubagentRunCard:
        run = self.get_subagent_run(run_id)
        if run.status != SubagentRunStatus.CREATED:
            return run
        run.status = SubagentRunStatus.RUNNING
        run.started_at = now_ts()
        self.save_subagent_run(run)
        self.append_event("subagent_run.started", run.task_id, {"run_id": run.run_id})
        return run

    # LLM: mark_subagent_completed stores outcome and artifacts before final parent delivery.
    # 函数用途: 标记子代理完成或失败，记录产物引用和是否仍需最终交付。
    def mark_subagent_completed(
        self,
        run_id: str,
        *,
        outcome: str,
        artifact_refs: list[str] | None = None,
        pending_final_delivery: bool = False,
    ) -> SubagentRunCard:
        run = self.get_subagent_run(run_id)
        run.status = SubagentRunStatus.COMPLETED if outcome == "ok" else SubagentRunStatus.FAILED
        run.outcome = outcome
        run.artifact_refs = list(artifact_refs or [])
        run.pending_final_delivery = bool(pending_final_delivery)
        run.ended_at = now_ts()
        self.save_subagent_run(run)
        self.append_event(
            "subagent_run.completed",
            run.task_id,
            {
                "run_id": run.run_id,
                "outcome": outcome,
                "artifact_refs": run.artifact_refs,
                "pending_final_delivery": run.pending_final_delivery,
            },
        )
        return run

    # LLM: mark_subagent_final_delivered clears the durable retry marker after parent delivery succeeds.
    # 函数用途: 标记子代理最终结果已送达父 session，不再需要恢复重试。
    def mark_subagent_final_delivered(self, run_id: str) -> SubagentRunCard:
        run = self.get_subagent_run(run_id)
        run.pending_final_delivery = False
        self.save_subagent_run(run)
        self.append_event("subagent_run.final_delivered", run.task_id, {"run_id": run.run_id})
        return run

    # LLM: mark_subagent_killed terminates a child-session run through the structured control plane.
    # 函数用途: 标记子代理被父级终止，清除待交付状态并保留终止原因。
    def mark_subagent_killed(self, run_id: str, *, reason: str = "") -> SubagentRunCard:
        run = self.get_subagent_run(run_id)
        run.status = SubagentRunStatus.KILLED
        run.outcome = "killed"
        run.pending_final_delivery = False
        run.ended_at = now_ts()
        run.metadata["kill_reason"] = reason
        self.save_subagent_run(run)
        self.append_event("subagent_run.killed", run.task_id, {"run_id": run.run_id, "reason": reason})
        return run

    # LLM: list_pending_final_delivery exposes subagent completions that still need parent notification.
    # 函数用途: 列出已完成但最终结果尚未送达父 session 的子代理运行记录。
    def list_pending_final_delivery(self) -> list[SubagentRunCard]:
        return [
            run
            for run in self.list_subagent_runs()
            if run.pending_final_delivery and run.status == SubagentRunStatus.COMPLETED
        ]

    # LLM: append_event writes append-only runtime facts for audit and recovery.
    # 函数用途: 向事件 JSONL 追加一条 EventCard。
    def append_event(self, event_type: str, task_id: str | None, payload: dict[str, Any]) -> EventCard:
        event = EventCard(event_id=new_card_id("event"), task_id=task_id, event_type=event_type, payload=_json_ready(payload))
        append_jsonl(self.events_path, event.to_dict(), sort_keys=True)
        return event

    # LLM: list_events replays event JSONL with optional task filtering.
    # 函数用途: 读取事件日志，可按 task_id 过滤。
    def list_events(self, task_id: str | None = None) -> list[EventCard]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events: list[EventCard] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = EventCard.from_dict(payload)
            if task_id is None or event.task_id == task_id:
                events.append(event)
        return events

    # LLM: check_integrity reports broken card references without repairing them implicitly.
    # 函数用途: 检查任务父子引用等结构问题，返回可诊断 findings。
    def check_integrity(self) -> list[dict[str, Any]]:
        task_ids = {task.task_id for task in self.list_tasks()}
        findings: list[dict[str, Any]] = []
        for task in self.list_tasks():
            for child_id in task.child_task_ids:
                if child_id not in task_ids:
                    findings.append({"code": "missing_child_task", "task_id": task.task_id, "child_task_id": child_id})
        return findings

    # LLM: _task_path centralizes task snapshot paths.
    # 函数用途: 生成 TaskCard 文件路径。
    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    # LLM: _lease_path centralizes lease snapshot paths.
    # 函数用途: 生成 LeaseCard 文件路径。
    def _lease_path(self, lease_id: str) -> Path:
        return self.leases_dir / f"{lease_id}.json"

    # LLM: _subagent_run_path centralizes run-registry snapshot paths.
    # 函数用途: 生成 SubagentRunCard 文件路径。
    def _subagent_run_path(self, run_id: str) -> Path:
        return self.subagent_runs_dir / f"{run_id}.json"

    # LLM: _checkpoint_dir centralizes checkpoint folders under a task.
    # 函数用途: 生成任务 checkpoint 目录路径。
    def _checkpoint_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "checkpoints"

    # LLM: _checkpoint_path centralizes checkpoint file paths.
    # 函数用途: 生成 CheckpointCard 文件路径。
    def _checkpoint_path(self, task_id: str, checkpoint_id: str) -> Path:
        return self._checkpoint_dir(task_id) / f"{checkpoint_id}.json"

    # LLM: lock exposes a small local critical section for cross-thread/process card updates.
    # 函数用途: 保护 session 绑定、租约和 worker slot 这类读改写流程。
    def lock(self, name: str, *, timeout_seconds: float = 5.0):
        return _file_lock(self.root / "locks" / f"{_safe_lock_name(name)}.lock", timeout_seconds=timeout_seconds)


 # LLM: _json_ready normalizes enum-rich payloads into JSON-compatible values.
 # 函数用途: 把事件 payload 转成可写入 JSONL 的普通结构。
def _json_ready(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


# LLM: _file_lock provides dependency-free local critical sections for file-backed cards.
# 函数用途: 用 O_EXCL lock 文件避免并发 main/worker 对同一资源读改写冲突。
@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 5.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {path}") from None
            time.sleep(0.005)
    try:
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# LLM: _safe_lock_name keeps lock filenames bounded to local filesystem-safe chars.
# 函数用途: 将 session/task/resource id 转为 lock 文件名。
def _safe_lock_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)
