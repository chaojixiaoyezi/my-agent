from __future__ import annotations

"""LLM: failure analysis service — all _analyze_* methods extracted here."""

from dataclasses import dataclass, field

from ..subagents.models import SubAgentRunnerResult, SubAgentTask


@dataclass
class FailureAnalysis:

    failure_type: str = ""
    root_cause: str = ""
    suggested_action: str = ""
    details: dict = field(default_factory=dict)
    should_retry: bool = False
    should_split: bool = False
    should_adjust_timeout: bool = False
    new_timeout_seconds: float | None = None
    split_suggestions: list[str] = field(default_factory=list)
    relevant_memories: list[str] = field(default_factory=list)


_MAX_TIMEOUT = 600.0
_MAX_RETRY_ATTEMPTS = 3


def _get_current_timeout(task: SubAgentTask) -> float:
    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        return float(task.attributes["dynamic_timeout_seconds"])
    return 120.0


def _suggest_splits(task: SubAgentTask) -> list[str]:
    if task.plan and len(task.plan) > 3:
        mid = len(task.plan) // 2
        return [f"前 {mid} 步：{task.plan[:mid]}", f"后 {len(task.plan) - mid} 步：{task.plan[mid:]}"]
    return _goal_based_split_suggestions(task.goal or "")


def _goal_based_split_suggestions(goal: str) -> list[str]:
    goal_lower = goal.lower()
    if "翻译" in goal_lower:
        return ["翻译前半部分", "翻译后半部分"]
    if "重构" in goal_lower:
        return ["重构数据结构", "重构业务逻辑"]
    if "分析" in goal_lower:
        return ["分析输入数据", "分析结果总结"]
    return ["执行第一部分", "执行第二部分"]


class FailureAnalysisService:

    def __init__(self, max_timeout: float = _MAX_TIMEOUT, max_retry_attempts: int = _MAX_RETRY_ATTEMPTS):
        self.max_timeout = max_timeout
        self.max_retry_attempts = max_retry_attempts

    def analyze_timeout(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts >= self.max_retry_attempts:
            return FailureAnalysis(
                failure_type="runner_timeout",
                root_cause="task_too_large",
                suggested_action="split_task",
                details={"attempts": task.runner_attempts, "runner_last_error": task.runner_last_error},
                should_retry=False, should_split=True, should_adjust_timeout=False,
                new_timeout_seconds=None, split_suggestions=_suggest_splits(task),
                relevant_memories=[],
            )
        current_timeout = _get_current_timeout(task)
        if current_timeout >= self.max_timeout:
            return FailureAnalysis(
                failure_type="runner_timeout",
                root_cause="task_too_large",
                suggested_action="split_task",
                details={"current_timeout": current_timeout, "max_timeout": self.max_timeout, "attempts": task.runner_attempts},
                should_retry=False, should_split=True, should_adjust_timeout=False,
                new_timeout_seconds=None, split_suggestions=_suggest_splits(task),
                relevant_memories=[],
            )
        new_timeout = min(current_timeout * 1.5, self.max_timeout)
        return FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="timeout",
            suggested_action="increase_timeout_and_retry",
            details={"current_timeout": current_timeout, "new_timeout": new_timeout, "attempts": task.runner_attempts},
            should_retry=True, should_split=False, should_adjust_timeout=True,
            new_timeout_seconds=new_timeout, split_suggestions=[],
            relevant_memories=[],
        )

    def analyze_capability(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.capability_grants:
            return FailureAnalysis(
                failure_type="capability_request",
                root_cause="insufficient_grant",
                suggested_action="manual_review",
                details={"grant_count": len(task.capability_grants), "request_count": len(task.capability_requests)},
                should_retry=False, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type="capability_request",
            root_cause="capability_missing",
            suggested_action="manual_capability_grant",
            details={"open_requests": len(task.capability_requests)},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_parse_error(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type="structured_output_parse_error",
                root_cause="parse_error",
                suggested_action="retry_with_same_timeout",
                details={"parse_error": runner_result.structured_parse_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type="structured_output_parse_error",
            root_cause="persistent_parse_error",
            suggested_action="manual_review",
            details={"parse_error": runner_result.structured_parse_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_tool_failure(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "tool_failure",
                root_cause="tool_transient_error",
                suggested_action="retry_with_same_timeout",
                details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=task.failure_type or "tool_failure",
            root_cause="persistent_tool_error",
            suggested_action="manual_review",
            details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_model_error(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "model_error",
                root_cause="model_transient_error",
                suggested_action="retry_with_same_timeout",
                details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=task.failure_type or "model_error",
            root_cause="persistent_model_error",
            suggested_action="manual_review",
            details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_channel_broken(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type="channel_broken",
            root_cause="channel_broken",
            suggested_action="manual_channel_repair",
            details={"channel_status": task.channel_status, "channel_checks": task.channel_checks},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_verification_failed(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type="verification_failed",
            root_cause="quality_issue",
            suggested_action="manual_review",
            details={"evidence_count": len(task.evidence), "runner_last_error": task.runner_last_error},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_generic_failure(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "unknown",
                root_cause="generic_failure",
                suggested_action="retry_with_same_timeout",
                details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=task.failure_type or "unknown",
            root_cause="persistent_failure",
            suggested_action="manual_review",
            details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )
