# LLM: Progress policies schedule reports but do not decide task quality.
# 模块用途: 管理定时汇报策略的创建、到期查询和汇报后推进。

from __future__ import annotations

from dataclasses import replace

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from .models import ProgressPolicy, new_id
from .store_common import now as current_time
from .store_wake import ConversationWakeStore


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
        data = read_json_file(self._policy_path(policy_id))
        return ProgressPolicy.from_dict(data) if data else None

    def list_progress_policies(self, *, enabled_only: bool = False) -> list[ProgressPolicy]:
        policies = [ProgressPolicy.from_dict(data) for path in sorted(self.policies_dir.glob("*.json")) if (data := read_json_file(path))]
        policies = [policy for policy in policies if policy.enabled] if enabled_only else policies
        policies.sort(key=lambda item: item.next_due_at)
        return policies

    def due_progress_policies(self, *, now: float | None = None) -> list[ProgressPolicy]:
        current = now if now is not None else __import__("time").time()
        return [policy for policy in self.list_progress_policies(enabled_only=True) if policy.next_due_at <= current]

    def mark_progress_reported(self, policy_id: str, *, now: float | None = None) -> ProgressPolicy:
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else __import__("time").time()
        updated = replace(policy, last_report_at=current, next_due_at=current + max(0, policy.interval_seconds))
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated
