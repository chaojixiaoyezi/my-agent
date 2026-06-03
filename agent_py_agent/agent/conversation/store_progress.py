
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import write_json_file_atomic
from ..runtime_errors import runtime_error_report
from .models import ProgressPolicy, new_id
from .store_common import now as current_time
from .store_wake import ConversationWakeStore


def _progress_policy_read_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.progress_policy.read")
    report["path"] = str(path)
    report["policy_id"] = path.stem
    return report


def _read_progress_policy_report(path: Path) -> tuple[ProgressPolicy | None, dict[str, Any] | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"progress policy is {type(data).__name__}, expected object")
        return ProgressPolicy.from_dict(data), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, _progress_policy_read_error(path, exc)


class ConversationProgressStore(ConversationWakeStore):
    def set_progress_policy(self, request: dict) -> ProgressPolicy:
        thread_id = str(request.get("thread_id") or "")
        self._require_thread(thread_id)
        current = current_time(request.get("now"))
        interval_seconds = max(0, int(request.get("interval_seconds") or 0))
        policy = ProgressPolicy(policy_id=new_id("policy"), thread_id=thread_id, task_id=str(request.get("task_id") or ""), interval_seconds=interval_seconds, next_due_at=current + interval_seconds, route_channel=str(request.get("route_channel") or "internal"), route_target=str(request.get("route_target") or ""), metadata=request.get("metadata") or {})
        write_json_file_atomic(self._policy_path(policy.policy_id), policy.to_dict())
        return policy

    def get_progress_policy(self, policy_id: str) -> ProgressPolicy | None:
        policy, _ = _read_progress_policy_report(self._policy_path(policy_id))
        return policy

    def list_progress_policies(self, *, enabled_only: bool = False) -> list[ProgressPolicy]:
        policies, _ = self.list_progress_policies_report(enabled_only=enabled_only)
        return policies

    def list_progress_policies_report(self, *, enabled_only: bool = False) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        policies: list[ProgressPolicy] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted(self.policies_dir.glob("*.json")):
            policy, error = _read_progress_policy_report(path)
            if policy is not None:
                policies.append(policy)
            if error is not None:
                load_errors.append(error)
        policies = [policy for policy in policies if policy.enabled] if enabled_only else policies
        policies.sort(key=lambda item: item.next_due_at)
        return policies, load_errors

    def due_progress_policies(self, *, now: float | None = None) -> list[ProgressPolicy]:
        policies, _ = self.due_progress_policies_report(now=now)
        return policies

    def due_progress_policies_report(self, *, now: float | None = None) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        current = now if now is not None else __import__("time").time()
        policies, load_errors = self.list_progress_policies_report(enabled_only=True)
        return [policy for policy in policies if policy.next_due_at <= current], load_errors

    def mark_progress_reported(self, policy_id: str, *, now: float | None = None) -> ProgressPolicy:
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else __import__("time").time()
        updated = replace(policy, last_report_at=current, next_due_at=current + max(0, policy.interval_seconds))
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated
