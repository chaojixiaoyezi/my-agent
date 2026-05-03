from __future__ import annotations

"""LLM: subagent failure analyzer.

给人看的解释：
分析子代理失败原因，给出重试、拆分或人工介入的建议。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentRunnerResult, SubAgentTask

if TYPE_CHECKING:
    pass


@dataclass
class FailureAnalysis:
    """失败分析结果。"""

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


class SubAgentFailureAnalyzer:
    """子代理失败分析器。"""

    def __init__(self, max_timeout: float = 600.0, max_retry_attempts: int = 3):
        self.max_timeout = max_timeout
        self.max_retry_attempts = max_retry_attempts

    def analyze(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析失败原因并给出建议。"""
        failure_type = task.failure_type or ""

        # 先获取相关记忆（推模式）
        relevant_memories = self._get_relevant_memories(failure_type, task)

        # 超时分析
        if failure_type == "runner_timeout":
            result = self._analyze_timeout(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 能力缺口分析
        if failure_type == "capability_request":
            result = self._analyze_capability(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 输出解析错误
        if failure_type == "structured_output_parse_error":
            result = self._analyze_parse_error(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 工具失败
        if failure_type in {"tool_result_missing", "tool_error"}:
            result = self._analyze_tool_failure(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 模型错误
        if failure_type in {"model_error", "api_error"}:
            result = self._analyze_model_error(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 通道损坏
        if task.channel_status == "BROKEN":
            result = self._analyze_channel_broken(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 验收失败
        if task.verification_status == "FAILED" and task.status == "BLOCKED":
            result = self._analyze_verification_failed(task, runner_result)
            result.relevant_memories = relevant_memories
            return result

        # 默认分析
        result = self._analyze_generic_failure(task, runner_result)
        result.relevant_memories = relevant_memories
        return result

    def _get_relevant_memories(self, failure_type: str, task: SubAgentTask) -> list[str]:
        """获取与失败类型相关的记忆（通过搜索已有记忆）。

        注意：这个方法只搜索记忆，不依赖 agent 实例。
        实际的记忆注入由 dispatch_mixin 完成。
        """
        return []  # 记忆注入由 dispatch_mixin 负责

    def _analyze_timeout(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析超时失败。"""

        # 检查是否已经多次超时
        if task.runner_attempts >= self.max_retry_attempts:
            # 多次超时，建议拆分
            return FailureAnalysis(
                failure_type="runner_timeout",
                root_cause="task_too_large",
                suggested_action="split_task",
                details={
                    "attempts": task.runner_attempts,
                    "runner_last_error": task.runner_last_error,
                },
                should_retry=False,
                should_split=True,
                should_adjust_timeout=False,
                new_timeout_seconds=None,
                split_suggestions=self._suggest_splits(task),
            )

        # 检查当前超时设置
        current_timeout = self._get_current_timeout(task)

        # 如果超时已经很大，建议拆分
        if current_timeout >= self.max_timeout:
            return FailureAnalysis(
                failure_type="runner_timeout",
                root_cause="task_too_large",
                suggested_action="split_task",
                details={
                    "current_timeout": current_timeout,
                    "max_timeout": self.max_timeout,
                    "attempts": task.runner_attempts,
                },
                should_retry=False,
                should_split=True,
                should_adjust_timeout=False,
                split_suggestions=self._suggest_splits(task),
            )

        # 首次或少数超时，建议提高超时后重试
        new_timeout = min(current_timeout * 1.5, self.max_timeout)

        return FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="timeout",
            suggested_action="increase_timeout_and_retry",
            details={
                "current_timeout": current_timeout,
                "new_timeout": new_timeout,
                "attempts": task.runner_attempts,
            },
            should_retry=True,
            should_split=False,
            should_adjust_timeout=True,
            new_timeout_seconds=new_timeout,
            split_suggestions=[],
        )

    def _analyze_capability(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析能力缺口。"""

        # 检查是否已有 grant
        if task.capability_grants:
            # 有授权但仍然失败，可能是授权不足
            return FailureAnalysis(
                failure_type="capability_request",
                root_cause="insufficient_grant",
                suggested_action="manual_review",
                details={
                    "grant_count": len(task.capability_grants),
                    "request_count": len(task.capability_requests),
                },
                should_retry=False,
                should_split=False,
                should_adjust_timeout=False,
                split_suggestions=[],
            )

        # 没有授权，需要人工介入
        return FailureAnalysis(
            failure_type="capability_request",
            root_cause="capability_missing",
            suggested_action="manual_capability_grant",
            details={
                "open_requests": len(task.capability_requests),
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_parse_error(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析输出解析错误。"""

        # 解析错误通常是临时问题，重试即可
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type="structured_output_parse_error",
                root_cause="parse_error",
                suggested_action="retry_with_same_timeout",
                details={
                    "parse_error": runner_result.structured_parse_error,
                    "attempts": task.runner_attempts,
                },
                should_retry=True,
                should_split=False,
                should_adjust_timeout=False,
                split_suggestions=[],
            )

        # 多次解析错误，需要人工介入
        return FailureAnalysis(
            failure_type="structured_output_parse_error",
            root_cause="persistent_parse_error",
            suggested_action="manual_review",
            details={
                "parse_error": runner_result.structured_parse_error,
                "attempts": task.runner_attempts,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_tool_failure(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析工具失败。"""

        # 工具失败通常是临时问题，重试即可
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "tool_failure",
                root_cause="tool_transient_error",
                suggested_action="retry_with_same_timeout",
                details={
                    "runner_last_error": task.runner_last_error,
                    "attempts": task.runner_attempts,
                },
                should_retry=True,
                should_split=False,
                should_adjust_timeout=False,
                split_suggestions=[],
            )

        # 多次工具失败，可能需要换工具或人工介入
        return FailureAnalysis(
            failure_type=task.failure_type or "tool_failure",
            root_cause="persistent_tool_error",
            suggested_action="manual_review",
            details={
                "runner_last_error": task.runner_last_error,
                "attempts": task.runner_attempts,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_model_error(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析模型错误。"""

        # 模型错误通常是临时问题，等待后重试
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "model_error",
                root_cause="model_transient_error",
                suggested_action="retry_with_same_timeout",
                details={
                    "runner_last_error": task.runner_last_error,
                    "attempts": task.runner_attempts,
                },
                should_retry=True,
                should_split=False,
                should_adjust_timeout=False,
                split_suggestions=[],
            )

        # 多次模型错误，需要人工介入
        return FailureAnalysis(
            failure_type=task.failure_type or "model_error",
            root_cause="persistent_model_error",
            suggested_action="manual_review",
            details={
                "runner_last_error": task.runner_last_error,
                "attempts": task.runner_attempts,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_channel_broken(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析通道损坏。"""

        return FailureAnalysis(
            failure_type="channel_broken",
            root_cause="channel_broken",
            suggested_action="manual_channel_repair",
            details={
                "channel_status": task.channel_status,
                "channel_checks": task.channel_checks,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_verification_failed(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析验收失败。"""

        # 验收失败，可能需要重做
        return FailureAnalysis(
            failure_type="verification_failed",
            root_cause="quality_issue",
            suggested_action="manual_review",
            details={
                "evidence_count": len(task.evidence),
                "runner_last_error": task.runner_last_error,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _analyze_generic_failure(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
        """分析通用失败。"""

        # 如果还有重试机会，建议重试
        if task.runner_attempts < self.max_retry_attempts:
            return FailureAnalysis(
                failure_type=task.failure_type or "unknown",
                root_cause="generic_failure",
                suggested_action="retry_with_same_timeout",
                details={
                    "runner_last_error": task.runner_last_error,
                    "attempts": task.runner_attempts,
                },
                should_retry=True,
                should_split=False,
                should_adjust_timeout=False,
                split_suggestions=[],
            )

        # 没有重试机会，需要人工介入
        return FailureAnalysis(
            failure_type=task.failure_type or "unknown",
            root_cause="persistent_failure",
            suggested_action="manual_review",
            details={
                "runner_last_error": task.runner_last_error,
                "attempts": task.runner_attempts,
            },
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
            split_suggestions=[],
        )

    def _get_current_timeout(self, task: SubAgentTask) -> float:
        """获取任务当前的超时设置。"""

        if task.attributes and "dynamic_timeout_seconds" in task.attributes:
            return float(task.attributes["dynamic_timeout_seconds"])
        return 120.0  # 默认超时

    def _suggest_splits(self, task: SubAgentTask) -> list[str]:
        """根据任务内容生成拆分建议。"""

        suggestions = []

        # 根据 plan 步骤拆分
        if task.plan and len(task.plan) > 3:
            mid = len(task.plan) // 2
            suggestions.append(f"前 {mid} 步：{task.plan[:mid]}")
            suggestions.append(f"后 {len(task.plan) - mid} 步：{task.plan[mid:]}")
        else:
            # 如果 plan 太短，按任务类型拆分
            goal_lower = (task.goal or "").lower()
            if "翻译" in goal_lower:
                suggestions.append("翻译前半部分")
                suggestions.append("翻译后半部分")
            elif "重构" in goal_lower:
                suggestions.append("重构数据结构")
                suggestions.append("重构业务逻辑")
            elif "分析" in goal_lower:
                suggestions.append("分析输入数据")
                suggestions.append("分析结果总结")
            else:
                suggestions.append("执行第一部分")
                suggestions.append("执行第二部分")

        return suggestions
