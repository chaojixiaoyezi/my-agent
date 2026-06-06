
from __future__ import annotations

"""Deterministic failure introspection for dispatch闭环."""

from dataclasses import dataclass, field

from ..subagents.models import SubAgentRunnerResult, SubAgentTask
from .failure_analysis_service import FailureAnalysis


@dataclass
class FailureIntrospection:

    analysis_reason: str = ""  # 人可读的失败原因分析
    root_cause: str = ""  # 根因分类
    suggested_params: dict = field(default_factory=dict)  # 建议调整的参数
    should_retry: bool = True  # 是否应该重试
    should_split: bool = False  # 是否应该拆分
    confidence: float = 0.5  # 分析置信度 0-1


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
        if task.attributes and "dynamic_timeout_seconds" in task.attributes:
            return float(task.attributes["dynamic_timeout_seconds"])
        return 120.0  # 默认超时
