# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Build findings for capability requests/gaps and output blockers.

新手说明:
检查是否还有未关闭的能力请求/缺口、output.json 里的 blocker。
"""

from ..models import SubAgentTask
from ..parsing import _string_list
from ..reports import AcceptanceReviewFinding


# LLM: _build_capability_and_blocker_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建能力blockerfindings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_capability_and_blocker_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """Build findings for capability requests/gaps and output blockers."""
    open_requests = [item for item in task.capability_requests if item.status == "OPEN"]
    open_gaps = [item for item in task.capability_gaps if item.status == "OPEN"]
    blockers = [item for item in _string_list(output.get("blockers", [])) if item.strip()]
    return [
        _capability_request_finding(task, open_requests, created_at),
        _capability_gap_finding(task, open_gaps, created_at),
        _output_blocker_finding(task, blockers, created_at),
    ]


# LLM: _capability_request_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理能力请求finding相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _capability_request_finding(task, open_requests, created_at):
    return (
        AcceptanceReviewFinding(
            name="no_open_capability_requests",
            ok=not open_requests,
            severity="P1",
            message=(
                "没有待处理 capability request。"
                if not open_requests
                else f"仍有 {len(open_requests)} 条 OPEN capability request。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )


# LLM: _capability_gap_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理能力缺口finding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _capability_gap_finding(task, open_gaps, created_at):
    return (
        AcceptanceReviewFinding(
            name="no_open_capability_gaps",
            ok=not open_gaps,
            severity="P1",
            message=(
                "没有待处理 capability gap。"
                if not open_gaps
                else f"仍有 {len(open_gaps)} 条 OPEN capability gap。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )


# LLM: _output_blocker_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理outputblockerfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _output_blocker_finding(task, blockers, created_at):
    return (
        AcceptanceReviewFinding(
            name="no_output_blockers",
            ok=not blockers,
            severity="P1",
            message="output.json 没有 blocker。" if not blockers else f"output.json 仍有 blocker: {blockers[0]}",
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
