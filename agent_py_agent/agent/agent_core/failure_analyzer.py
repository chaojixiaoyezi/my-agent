# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""subagent failure analyzer — delegates to failure_analysis_service.

分析子代理失败原因，给出重试、拆分或人工介入的建议。
业务逻辑已移至 failure_analysis_service.py。
"""

from ..subagents.models import SubAgentTask
from .failure_analysis_service import FailureAnalysis, FailureAnalysisService, _suggest_splits


# LLM: SubAgentFailureAnalyzer 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装subagent失败分析器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SubAgentFailureAnalyzer:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, max_timeout: float = 600.0, max_retry_attempts: int = 3):
        self._service = FailureAnalysisService(max_timeout, max_retry_attempts)

    # LLM: analyze 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def analyze(
        self,
        task: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> FailureAnalysis:
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

    # LLM: _suggest_splits 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理suggestsplits相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _suggest_splits(self, task: SubAgentTask) -> list[str]:
        return _suggest_splits(task)

    # LLM: _wrap_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理wrap结果相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _wrap_result(self, result: FailureAnalysis) -> FailureAnalysis:
        result.relevant_memories = []
        return result