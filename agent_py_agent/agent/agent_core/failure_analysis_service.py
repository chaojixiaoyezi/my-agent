# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""failure analysis service — all _analyze_* methods extracted here."""

from dataclasses import dataclass, field

from ..subagents.models import SubAgentRunnerResult, SubAgentTask


# LLM: FailureAnalysis 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存失败分析字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
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


# LLM: _get_current_timeout 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询current超时需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _get_current_timeout(task: SubAgentTask) -> float:
    if task.attributes and "dynamic_timeout_seconds" in task.attributes:
        return float(task.attributes["dynamic_timeout_seconds"])
    return 120.0


# LLM: _suggest_splits 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理suggestsplits相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _suggest_splits(task: SubAgentTask) -> list[str]:
    if task.plan and len(task.plan) > 3:
        mid = len(task.plan) // 2
        return [f"前 {mid} 步：{task.plan[:mid]}", f"后 {len(task.plan) - mid} 步：{task.plan[mid:]}"]
    return ["执行第一部分", "执行第二部分"]


# LLM: FailureAnalysisService 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装失败分析服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class FailureAnalysisService:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, max_timeout: float = _MAX_TIMEOUT, max_retry_attempts: int = _MAX_RETRY_ATTEMPTS):
        self.max_timeout = max_timeout
        self.max_retry_attempts = max_retry_attempts

    # LLM: analyze_timeout 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze超时相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: analyze_capability 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze能力相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: analyze_parse_error 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze解析error相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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

    # LLM: analyze_tool_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze工具失败相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: analyze_model_error 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze模型error相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: analyze_channel_broken 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyze通道broken相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def analyze_channel_broken(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type="channel_broken",
            root_cause="channel_broken",
            suggested_action="manual_channel_repair",
            details={"channel_status": task.channel_status, "channel_checks": task.channel_checks},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    # LLM: analyze_verification_failed 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyzeverificationfailed相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def analyze_verification_failed(self, task: SubAgentTask, runner_result: SubAgentRunnerResult) -> FailureAnalysis:
        return FailureAnalysis(
            failure_type="verification_failed",
            root_cause="quality_issue",
            suggested_action="manual_review",
            details={"evidence_count": len(task.evidence), "runner_last_error": task.runner_last_error},
            should_retry=False, should_split=False, should_adjust_timeout=False,
            split_suggestions=[], relevant_memories=[],
        )

    # LLM: analyze_generic_failure 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理analyzegeneric失败相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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
