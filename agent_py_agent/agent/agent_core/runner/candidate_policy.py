
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerCandidatePolicy:
    runner_max_attempts: int = 1
    same_run_redispatch_limit: int | None = None
    background_launch_id: str = ""


def candidate_policy(policy: RunnerCandidatePolicy | None = None) -> RunnerCandidatePolicy:
    return policy or RunnerCandidatePolicy()


def runner_launch_in_progress(task: object, policy: RunnerCandidatePolicy) -> bool:
    if _runner_active_attempt_id(task):
        return True
    return _background_start_active(task, background_launch_id=policy.background_launch_id)


def _runner_active_attempt_id(task: object) -> str:
    value = getattr(task, "runner_active_attempt_id", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _background_start_active(task: object, *, background_launch_id: str = "") -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return False
    status = str(background.get("status") or "").strip().lower()
    if background_launch_id and str(background.get("launch_id") or "").strip() == str(background_launch_id).strip():
        return False
    return status in {"launching", "running"}


__all__ = ["RunnerCandidatePolicy", "candidate_policy", "runner_launch_in_progress"]
