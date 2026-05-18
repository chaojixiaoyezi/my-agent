from __future__ import annotations

# LLM: CardStore is the local durable fact store for cards; preserve JSON shapes and append-only event semantics.
# 模块用途: 提供 Card 的文件持久化、状态迁移、租约、恢复点、事件和完整性检查。
import json
from pathlib import Path
from typing import Any

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from ..io import append_jsonl
from .models import (
    VALID_TASK_TRANSITIONS,
    CheckpointCard,
    EventCard,
    LeaseCard,
    NotificationRouteCard,
    ProgressPolicyCard,
    SessionCard,
    TaskCard,
    TaskStatus,
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
        session = self.get_session(session_id)
        if task_id not in session.active_task_ids:
            session.active_task_ids.append(task_id)
        self.save_session(session)
        self.append_event("session.task_attached", task_id, {"session_id": session_id})
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
        return tasks

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

    # LLM: _checkpoint_dir centralizes checkpoint folders under a task.
    # 函数用途: 生成任务 checkpoint 目录路径。
    def _checkpoint_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "checkpoints"

    # LLM: _checkpoint_path centralizes checkpoint file paths.
    # 函数用途: 生成 CheckpointCard 文件路径。
    def _checkpoint_path(self, task_id: str, checkpoint_id: str) -> Path:
        return self._checkpoint_dir(task_id) / f"{checkpoint_id}.json"


 # LLM: _json_ready normalizes enum-rich payloads into JSON-compatible values.
 # 函数用途: 把事件 payload 转成可写入 JSONL 的普通结构。
def _json_ready(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
