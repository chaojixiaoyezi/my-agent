# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Helpers for evidence-related acceptance review findings."""

from dataclasses import dataclass

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding


# LLM: FindingParams 属于子代理验收证据的类边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 类用途: 集中保存finding参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class FindingParams:

    name: str
    ok: bool
    severity: str
    message: str
    evidence_path: str
    created_at: float


# LLM: ToolEvidenceParams 属于子代理验收证据的类边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 类用途: 集中保存工具证据参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ToolEvidenceParams:

    tool_name: str
    kind_aliases: set[str]
    used_tools: list[str]
    evidence: list


# LLM: _make_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建finding所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _make_finding(
    params: FindingParams | None = None,
    *,
    name: str = "",
    ok: bool = False,
    severity: str = "",
    message: str = "",
    evidence_path: str = "",
    created_at: float = 0.0,
) -> AcceptanceReviewFinding:
    """Create a single AcceptanceReviewFinding."""
    params = params or FindingParams(name, ok, severity, message, evidence_path, created_at)
    return AcceptanceReviewFinding(
        name=params.name,
        ok=params.ok,
        severity=params.severity,
        message=params.message,
        evidence_path=params.evidence_path,
        created_at=params.created_at,
    )


# LLM: _has_tool_evidence 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 判断工具证据条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _has_tool_evidence(
    params: ToolEvidenceParams,
) -> bool:
    """Check if tool has evidence in the task evidence list."""
    if params.tool_name not in params.used_tools:
        return False
    return any(
        item.ok and (
            item.kind in params.kind_aliases
            or params.tool_name in item.command.lower()
        )
        for item in params.evidence
    )


# LLM: build_evidence_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 兼容旧 import 路径，实际证据判断统一交给 service 层；关键副作用: 本函数不写文件，只返回验收 findings。
def build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    from ..services.acceptance_evidence_findings import (
        build_evidence_findings as service_build_evidence_findings,
    )

    return service_build_evidence_findings(task, created_at)
