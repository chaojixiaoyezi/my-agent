from __future__ import annotations

"""LLM: subagent failure analyzer — delegates to failure_analysis_service.

给人看的解释：
分析子代理失败原因，给出重试、拆分或人工介入的建议。
业务逻辑已移至 failure_analysis_service.py。
"""

from ..subagents.models import SubAgentTask
from .failure_analysis_service import FailureAnalysis, FailureAnalysisService, _suggest_splits


class SubAgentFailureAnalyzer:
    """子代理失败分析器 — thin facade delegating to FailureAnalysisService."""

    def __init__(self, max_timeout: float = 600.0, max_retry_attempts: int = 3):
        self._service = FailureAnalysisService(max_timeout, max_retry_attempts)

    def analyze(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析失败原因并给出建议。"""
        failure_type = task.failure_type or ""

        if failure_type == "runner_timeout":
            return self._wrap_result(self._service.analyze_timeout(task, runner_result))
        if failure_type == "capability_request":
            return self._wrap_result(self._service.analyze_capability(task, runner_result))
        if failure_type == "structured_output_parse_error":
            return self._wrap_result(self._service.analyze_parse_error(task, runner_result))
        if failure_type in {"tool_result_missing", "tool_error"}:
            return self._wrap_result(self._service.analyze_tool_failure(task, runner_result))
        if failure_type in {"model_error", "api_error"}:
            return self._wrap_result(self._service.analyze_model_error(task, runner_result))
        if task.channel_status == "BROKEN":
            return self._wrap_result(self._service.analyze_channel_broken(task, runner_result))
        if task.verification_status == "FAILED" and task.status == "BLOCKED":
            return self._wrap_result(self._service.analyze_verification_failed(task, runner_result))

        return self._wrap_result(self._service.analyze_generic_failure(task, runner_result))

    def _suggest_splits(self, task: SubAgentTask) -> list[str]:
        """Expose _suggest_splits for backward compatibility with tests."""
        return _suggest_splits(task)

    def _wrap_result(self, result: FailureAnalysis) -> FailureAnalysis:
        result.relevant_memories = []
        return result