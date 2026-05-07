# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""build readiness, channel, and structured-output findings.

新手说明:
检查任务是否处于等待验收状态、通道是否正常、runner 结构化输出是否可解析。
"""

from dataclasses import dataclass

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding
from .evidence import _make_finding


# LLM: StructuredFindingParams 属于子代理验收证据的类边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 类用途: 集中保存structuredfinding参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class StructuredFindingParams:

    task: SubAgentTask
    runner: dict[str, object]
    found: bool
    ok: bool
    created_at: float


# LLM: _structured_output_message 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理structuredoutput消息相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持验收证据、补丁摘要和就绪判断上的返回值和副作用边界稳定。
def _structured_output_message(
    runner: dict[str, object],
    found: bool,
    ok: bool,
) -> str:
    """Build message string for structured output finding."""
    if found and ok:
        return "runner 结构化输出可解析。"
    if not found:
        return "runner 未记录结构化输出，按人工证据验收。"
    return f"runner 结构化输出解析失败: {runner.get('structured_parse_error', '')}"


# LLM: _build_readiness_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建就绪findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_readiness_findings(
    task: SubAgentTask,
    runner: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:

    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    runner_structured_found = bool(runner.get("structured_output_found", False))
    runner_structured_ok = bool(runner.get("structured_output_ok", False))
    return [
        _ready_finding(task, ready, created_at),
        _channel_finding(task, created_at),
        _structured_finding(StructuredFindingParams(task, runner, runner_structured_found, runner_structured_ok, created_at)),
    ]


# LLM: _ready_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 读取或查询readyfinding需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _ready_finding(task, ready, created_at):
    return _make_finding(
        name="ready_for_acceptance",
        ok=ready,
        severity="P1",
        message=(
            "任务处于等待验收状态。"
            if ready
            else f"任务未处于等待验收状态: status={task.status} verify={task.verification_status}"
        ),
        evidence_path=task.runner_result_json,
        created_at=created_at,
    )


# LLM: _channel_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理通道finding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _channel_finding(task, created_at):
    return (
        _make_finding(
            name="channel_not_broken",
            ok=task.channel_status != "BROKEN",
            severity="P1",
            message=(
                "通道未标记为 BROKEN。"
                if task.channel_status != "BROKEN"
                else "通道为 BROKEN，不能验收。"
            ),
            evidence_path=task.channel_probe_file,
            created_at=created_at,
        )
    )


# LLM: _structured_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理structuredfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _structured_finding(params: StructuredFindingParams):
    return (
        _make_finding(
            name="structured_output",
            ok=(not params.found) or params.ok,
            severity="P1",
            message=_structured_output_message(params.runner, params.found, params.ok),
            evidence_path=params.task.runner_result_json,
            created_at=params.created_at,
        )
    )
