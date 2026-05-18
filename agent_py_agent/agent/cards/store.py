from __future__ import annotations

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


class CardStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.sessions_dir = self.root / "sessions"
        self.leases_dir = self.root / "leases"
        self.routes_dir = self.root / "notification_routes"
        self.policies_dir = self.root / "progress_policies"
        self.events_path = self.root / "events.jsonl"

    def save_session(self, session: SessionCard) -> None:
        session.updated_at = now_ts()
        write_json_file_atomic(self.sessions_dir / f"{session.session_id}.json", session.to_dict())

    def get_session(self, session_id: str) -> SessionCard:
        payload = read_json_file(self.sessions_dir / f"{session_id}.json")
        if not payload:
            raise KeyError(session_id)
        return SessionCard.from_dict(payload)

    def attach_task_to_session(self, session_id: str, task_id: str) -> SessionCard:
        session = self.get_session(session_id)
        if task_id not in session.active_task_ids:
            session.active_task_ids.append(task_id)
        self.save_session(session)
        self.append_event("session.task_attached", task_id, {"session_id": session_id})
        return session

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

    def save_task(self, task: TaskCard) -> None:
        task.updated_at = now_ts()
        write_json_file_atomic(self._task_path(task.task_id), task.to_dict())

    def get_task(self, task_id: str) -> TaskCard:
        payload = read_json_file(self._task_path(task_id))
        if not payload:
            raise KeyError(task_id)
        return TaskCard.from_dict(payload)

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

    def get_active_lease(self, resource_type: str, resource_id: str) -> LeaseCard | None:
        for lease in self.list_leases():
            if lease.resource_type == resource_type and lease.resource_id == resource_id and lease.is_active:
                return lease
        return None

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

    def save_checkpoint(self, checkpoint: CheckpointCard) -> None:
        checkpoint.updated_at = now_ts()
        write_json_file_atomic(self._checkpoint_path(checkpoint.task_id, checkpoint.checkpoint_id), checkpoint.to_dict())
        self.append_event("checkpoint.saved", checkpoint.task_id, {"checkpoint_id": checkpoint.checkpoint_id, "step": checkpoint.step})

    def latest_checkpoint(self, task_id: str) -> CheckpointCard | None:
        checkpoint_dir = self._checkpoint_dir(task_id)
        try:
            paths = sorted(checkpoint_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
        except OSError:
            return None
        if not paths:
            return None
        return CheckpointCard.from_dict(read_json_file(paths[-1]))

    def save_notification_route(self, route: NotificationRouteCard) -> None:
        route.updated_at = now_ts()
        write_json_file_atomic(self.routes_dir / f"{route.task_id}.json", route.to_dict())

    def get_notification_route(self, task_id: str) -> NotificationRouteCard | None:
        payload = read_json_file(self.routes_dir / f"{task_id}.json")
        return NotificationRouteCard.from_dict(payload) if payload else None

    def save_progress_policy(self, policy: ProgressPolicyCard) -> None:
        policy.updated_at = now_ts()
        write_json_file_atomic(self.policies_dir / f"{policy.task_id}.json", policy.to_dict())

    def get_progress_policy(self, task_id: str) -> ProgressPolicyCard | None:
        payload = read_json_file(self.policies_dir / f"{task_id}.json")
        return ProgressPolicyCard.from_dict(payload) if payload else None

    def append_event(self, event_type: str, task_id: str | None, payload: dict[str, Any]) -> EventCard:
        event = EventCard(event_id=new_card_id("event"), task_id=task_id, event_type=event_type, payload=_json_ready(payload))
        append_jsonl(self.events_path, event.to_dict(), sort_keys=True)
        return event

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

    def check_integrity(self) -> list[dict[str, Any]]:
        task_ids = {task.task_id for task in self.list_tasks()}
        findings: list[dict[str, Any]] = []
        for task in self.list_tasks():
            for child_id in task.child_task_ids:
                if child_id not in task_ids:
                    findings.append({"code": "missing_child_task", "task_id": task.task_id, "child_task_id": child_id})
        return findings

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _lease_path(self, lease_id: str) -> Path:
        return self.leases_dir / f"{lease_id}.json"

    def _checkpoint_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "checkpoints"

    def _checkpoint_path(self, task_id: str, checkpoint_id: str) -> Path:
        return self._checkpoint_dir(task_id) / f"{checkpoint_id}.json"


def _json_ready(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))
