from __future__ import annotations

"""LLM: LLM-based failure introspection for dispatch闭环.

给人看的解释：
在规则分类器（SubAgentFailureAnalyzer）之后，增加 LLM 自省层。
分析失败"为什么"发生，给出调参建议，并注入下一轮 task。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentRunnerResult, SubAgentTask
from .failure_analyzer import FailureAnalysis

if TYPE_CHECKING:
    from ..core import SimpleAgent

logger = logging.getLogger(__name__)


@dataclass
class FailureIntrospection:

    analysis_reason: str = ""  # 人可读的失败原因分析
    root_cause: str = ""  # 根因分类
    suggested_params: dict = field(default_factory=dict)  # 建议调整的参数
    should_retry: bool = True  # 是否应该重试
    should_split: bool = False  # 是否应该拆分
    confidence: float = 0.5  # 分析置信度 0-1


class FailureIntrospector:

    def __init__(self, agent: SimpleAgent | None = None) -> None:
        self._agent = agent

    def set_agent(self, agent: SimpleAgent) -> None:
        self._agent = agent

    def introspect(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
        failure_analysis: FailureAnalysis,
    ) -> FailureIntrospection:
        if self._agent is None:
            logger.warning("FailureIntrospector: agent 未设置，降级到规则分类")
            return self._fallback_to_rules(failure_analysis)

        try:
            return self._call_llm_introspect(task, runner_result, failure_analysis)
        except Exception as exc:
            logger.warning(f"FailureIntrospector: LLM 调用失败，降级到规则分类: {exc}")
            return self._fallback_to_rules(failure_analysis)

    def _call_llm_introspect(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
        failure_analysis: FailureAnalysis,
    ) -> FailureIntrospection:
        current_timeout = self._get_current_timeout(task)
        tool_rounds = getattr(runner_result, "tool_rounds", 0)
        error_msg = runner_result.runner_last_error or runner_result.message or ""

        prompt = f"""分析以下任务失败原因，给出调参建议：

任务目标: {(task.goal or "")[:200]}
失败类型: {failure_analysis.failure_type}
规则分类根因: {failure_analysis.root_cause}
规则建议动作: {failure_analysis.suggested_action}
当前参数:
  - 超时: {current_timeout}秒
  - runner_attempts: {task.runner_attempts}
  - tool_rounds: {tool_rounds}

错误信息: {error_msg[:300]}

请分析：
1. 为什么失败？（文件太大？模型太慢？超时太短？工具缺失？prompt 太复杂？）
2. 建议怎么调参？（提高超时？简化 prompt？增加 tool_rounds？拆分任务？）
3. 是否应该重试？是否应该拆分？

输出严格 JSON 格式：
{{
  "analysis_reason": "任务太大，tool_rounds 耗尽仍没完成，需要拆分",
  "root_cause": "task_too_complex",
  "suggested_params": {{"new_timeout_seconds": 300, "max_tool_rounds": 15}},
  "should_retry": true,
  "should_split": false,
  "confidence": 0.85
}}

只输出 JSON，不要其他文字。"""

        try:
            response = self._agent.run(prompt, save=False)
            # 解析 JSON
            result_data = json.loads(response.response.strip())
            return FailureIntrospection(
                analysis_reason=str(result_data.get("analysis_reason", "")),
                root_cause=str(result_data.get("root_cause", failure_analysis.root_cause)),
                suggested_params=dict(result_data.get("suggested_params", {})),
                should_retry=bool(result_data.get("should_retry", failure_analysis.should_retry)),
                should_split=bool(result_data.get("should_split", failure_analysis.should_split)),
                confidence=float(result_data.get("confidence", 0.5)),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(f"FailureIntrospector: JSON 解析失败: {exc}，降级到规则分类")
            return self._fallback_to_rules(failure_analysis)

    def _fallback_to_rules(self, failure_analysis: FailureAnalysis) -> FailureIntrospection:
        return FailureIntrospection(
            analysis_reason=f"规则分类：{failure_analysis.suggested_action}",
            root_cause=failure_analysis.root_cause,
            suggested_params=self._suggest_params_from_analysis(failure_analysis),
            should_retry=failure_analysis.should_retry,
            should_split=failure_analysis.should_split,
            confidence=0.3,  # 低置信度表示是降级结果
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