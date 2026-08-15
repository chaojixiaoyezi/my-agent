
from __future__ import annotations

"""failure analysis service — all _analyze_* methods extracted here."""

import time
from dataclasses import dataclass, field

from ..subagents.models import FailureType, SubAgentRunnerResult, SubAgentTask, TaskStatus


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


@dataclass
class FailureIntrospection:

    analysis_reason: str = ""
    root_cause: str = ""
    suggested_params: dict = field(default_factory=dict)
    should_retry: bool = True
    should_split: bool = False
    confidence: float = 0.5


class FailureIntrospector:

    def introspect(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
        failure_analysis: FailureAnalysis,
    ) -> FailureIntrospection:
        return self._from_rules(failure_analysis)

    def _from_rules(self, failure_analysis: FailureAnalysis) -> FailureIntrospection:
        return FailureIntrospection(
            analysis_reason=f"规则分类：{failure_analysis.suggested_action}",
            root_cause=failure_analysis.root_cause,
            suggested_params=self._suggest_params_from_analysis(failure_analysis),
            should_retry=failure_analysis.should_retry,
            should_split=failure_analysis.should_split,
            confidence=0.55,
        )

    def _suggest_params_from_analysis(self, analysis: FailureAnalysis) -> dict:
        params = {}
        if analysis.should_adjust_timeout and analysis.new_timeout_seconds:
            params["new_timeout_seconds"] = analysis.new_timeout_seconds
        if analysis.should_split and analysis.split_suggestions:
            params["split_suggestions"] = analysis.split_suggestions
        return params

    def _get_current_timeout(self, task: SubAgentTask) -> float:
        return _get_current_timeout(task)


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
    return ["执行第一部分", "执行第二部分"]


class FailureAnalysisService:

    def __init__(self, max_timeout: float = _MAX_TIMEOUT, max_retry_attempts: int = _MAX_RETRY_ATTEMPTS):
        self.max_timeout = max_timeout
        self.max_retry_attempts = max_retry_attempts

    def analyze_timeout(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts >= self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=FailureType.RUNNER_TIMEOUT.value,
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
                failure_type=FailureType.RUNNER_TIMEOUT.value,
                root_cause="task_too_large",
                suggested_action="split_task",
                details={"current_timeout": current_timeout, "max_timeout": self.max_timeout, "attempts": task.runner_attempts},
                should_retry=False, should_split=True, should_adjust_timeout=False,
                new_timeout_seconds=None, split_suggestions=_suggest_splits(task),
                relevant_memories=[],
            )
        new_timeout = min(current_timeout * 1.5, self.max_timeout)
        return FailureAnalysis(
            failure_type=FailureType.RUNNER_TIMEOUT.value,
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
                failure_type=FailureType.CAPABILITY_REQUEST.value,
                root_cause="insufficient_grant",
                suggested_action="manual_review",
                details={"grant_count": len(task.capability_grants), "request_count": len(task.capability_requests)},
                should_retry=False, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=FailureType.CAPABILITY_REQUEST.value,
            root_cause="capability_missing",
            suggested_action="manual_capability_grant",
            details={"open_requests": len(task.capability_requests)},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_parse_error(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value,
                root_cause="parse_error",
                suggested_action="retry_with_same_timeout",
                details={"parse_error": runner_result.structured_parse_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value,
            root_cause="persistent_parse_error",
            suggested_action="manual_review",
            details={"parse_error": runner_result.structured_parse_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_tool_failure(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or FailureType.TOOL_FAILURE.value,
                root_cause="tool_transient_error",
                suggested_action="retry_with_same_timeout",
                details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=task.failure_type or FailureType.TOOL_FAILURE.value,
            root_cause="persistent_tool_error",
            suggested_action="manual_review",
            details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_model_error(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or FailureType.MODEL_ERROR.value,
                root_cause="model_transient_error",
                suggested_action="retry_with_same_timeout",
                details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
                should_retry=True, should_split=False, should_adjust_timeout=False,
                split_suggestions=[], relevant_memories=[],
            )
        return FailureAnalysis(
            failure_type=task.failure_type or FailureType.MODEL_ERROR.value,
            root_cause="persistent_model_error",
            suggested_action="manual_review",
            details={"runner_last_error": task.runner_last_error, "attempts": task.runner_attempts},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_channel_broken(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type=FailureType.CHANNEL_BROKEN.value,
            root_cause="channel_broken",
            suggested_action="manual_channel_repair",
            details={"channel_status": task.channel_status, "channel_checks": task.channel_checks},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    def analyze_verification_failed(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type=FailureType.VERIFICATION_FAILED.value,
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


class SubAgentFailureAnalyzer:
    def __init__(self, max_timeout: float = _MAX_TIMEOUT, max_retry_attempts: int = _MAX_RETRY_ATTEMPTS):
        self._service = FailureAnalysisService(max_timeout, max_retry_attempts)

    def analyze(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        failure_type = task.failure_type or ""

        if failure_type == FailureType.RUNNER_TIMEOUT.value:
            return self._service.analyze_timeout(task, runner_result)
        if failure_type == FailureType.CAPABILITY_REQUEST.value:
            return self._service.analyze_capability(task, runner_result)
        if failure_type == FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value:
            return self._service.analyze_parse_error(task, runner_result)
        if failure_type in {FailureType.TOOL_RESULT_MISSING.value, FailureType.TOOL_ERROR.value}:
            return self._service.analyze_tool_failure(task, runner_result)
        if failure_type in {FailureType.MODEL_ERROR.value, FailureType.API_ERROR.value}:
            return self._service.analyze_model_error(task, runner_result)
        if task.channel_status == "BROKEN":
            return self._service.analyze_channel_broken(task, runner_result)
        if task.verification_status == "FAILED" and task.status == "BLOCKED":
            return self._service.analyze_verification_failed(task, runner_result)

        return self._service.analyze_generic_failure(task, runner_result)

    def suggest_splits(self, task: SubAgentTask) -> list[str]:
        return _suggest_splits(task)

    def _suggest_splits(self, task: SubAgentTask) -> list[str]:
        return self.suggest_splits(task)


def adaptive_retry(
    task: SubAgentTask,
    analysis: FailureAnalysis,
    max_split_depth: int = 2,
) -> SubAgentTask | list[SubAgentTask]:
    if not analysis.should_retry and not analysis.should_split:
        return []

    if analysis.should_split:
        if task.depth >= max_split_depth:
            return []
        return split_task(task, analysis.split_suggestions)

    if analysis.should_adjust_timeout and analysis.new_timeout_seconds:
        task.attributes["dynamic_timeout_seconds"] = analysis.new_timeout_seconds
        task.runner_attempts = 0
        task.status = "PLANNING"
        task.failure_type = ""
        return [task]

    task.runner_attempts = 0
    task.status = "PLANNING"
    task.failure_type = ""
    return [task]


def split_task(task: SubAgentTask, suggestions: list[str]) -> list[SubAgentTask]:
    subtasks = [
        _build_split_subtask(task, suggestions, index, suggestion)
        for index, suggestion in enumerate(suggestions)
    ]
    _mark_task_split(task, subtasks)
    return subtasks


def _build_split_subtask(
    task: SubAgentTask,
    suggestions: list[str],
    index: int,
    suggestion: str,
) -> SubAgentTask:
    subtask = SubAgentTask(
        id=f"{task.id}-part-{index+1:02d}",
        goal=f"{task.goal} - 第{index+1}部分：{suggestion}",
        thought=task.thought,
        plan=_split_subtask_plan(task, suggestions, index),
        agent_name=task.agent_name,
        role=task.role,
        owner=task.owner,
        supervisor=task.supervisor,
        parent_id=task.id,
        root_id=task.root_id or task.id,
        depth=task.depth + 1,
        allowed_skills=list(task.allowed_skills),
        allowed_tools=list(task.allowed_tools),
        created_at=time.time(),
        updated_at=time.time(),
    )
    if task.context_manifest:
        subtask.context_manifest = task.context_manifest
    return subtask


def _split_subtask_plan(task: SubAgentTask, suggestions: list[str], index: int) -> list[str]:
    if task.plan and suggestions:
        step_per_subtask = max(1, len(task.plan) // len(suggestions))
        start_idx = index * step_per_subtask
        return task.plan[start_idx : start_idx + step_per_subtask]
    return task.plan if index == 0 else []


def _mark_task_split(task: SubAgentTask, subtasks: list[SubAgentTask]) -> None:
    task.status = TaskStatus.TAKEN_OVER.value
    task.attributes["split_into"] = [subtask.id for subtask in subtasks]
    task.child_ids = [subtask.id for subtask in subtasks]
    task.updated_at = time.time()


def should_auto_split(task: SubAgentTask, max_depth: int = 2) -> bool:
    if task.depth >= max_depth:
        return False
    if len(task.plan) <= 3:
        return False
    return task.failure_type == FailureType.RUNNER_TIMEOUT.value and task.runner_attempts >= 2


def estimate_split_count(task: SubAgentTask) -> int:
    plan_length = len(task.plan)
    if plan_length <= 3:
        return 1
    if plan_length <= 6:
        return 2
    if plan_length <= 10:
        return 3
    return max(3, plan_length // 4)
