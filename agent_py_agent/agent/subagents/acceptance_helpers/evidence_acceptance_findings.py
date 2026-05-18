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
# 函数用途: 构建证据findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """Build all evidence acceptance findings for a task."""
    findings = _build_presence_findings(task, created_at)
    findings.extend(_build_tool_requirement_findings(task, created_at))
    return findings


# LLM: _build_presence_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建presencefindings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_presence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    ok_evidence = [item for item in task.evidence if item.ok]
    bad_evidence = [item for item in task.evidence if not item.ok]

    return [
        _make_finding(
            name="evidence_present",
            ok=bool(ok_evidence),
            severity="P0",
            message=(
                f"已有 {len(ok_evidence)} 条可用验收证据。"
                if ok_evidence
                else "缺少可用验收证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
        _make_finding(
            name="evidence_not_failed",
            ok=not bad_evidence,
            severity="P1",
            message=(
                "没有失败验收证据。"
                if not bad_evidence
                else f"存在 {len(bad_evidence)} 条失败证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
    ]


# LLM: _build_tool_requirement_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建工具requirementfindings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_tool_requirement_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    findings: list[AcceptanceReviewFinding] = []
    required_tools = _required_tool_evidence(task)

    if "read_file" in required_tools:
        findings.append(
            _build_read_file_requirement_finding(task, created_at)
        )
    if "write_file" in required_tools:
        findings.append(
            _build_write_file_requirement_finding(task, created_at)
        )

    return findings


# LLM: _build_read_file_requirement_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建文件requirementfinding所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_read_file_requirement_finding(
    task: SubAgentTask,
    created_at: float,
) -> AcceptanceReviewFinding:
    has_read = _has_tool_evidence(
        ToolEvidenceParams(
            "read_file",
            {"read_file", "file_read", "file_content"},
            task.used_tools,
            task.evidence,
        )
    )
    return _make_finding(
        name="acceptance_requires_read_file",
        ok=has_read,
        severity="P0",
        message=(
            "结构化工具证据合同要求 read_file，且已有对应工具和证据。"
            if has_read
            else "结构化工具证据合同要求 read_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )


# LLM: _build_write_file_requirement_finding 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建文件requirementfinding所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动验收证据、补丁摘要和就绪判断，调用方依赖写入顺序和文件格式。
def _build_write_file_requirement_finding(
    task: SubAgentTask,
    created_at: float,
) -> AcceptanceReviewFinding:
    has_write = _has_tool_evidence(
        ToolEvidenceParams(
            "write_file",
            {"write_file", "file_write", "file_written"},
            task.used_tools,
            task.evidence,
        )
    )
    return _make_finding(
        name="acceptance_requires_write_file",
        ok=has_write,
        severity="P0",
        message=(
            "结构化工具证据合同要求 write_file，且已有对应工具和证据。"
            if has_write
            else "结构化工具证据合同要求 write_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )


# LLM: _required_tool_evidence keeps the legacy helper aligned with the structured contract.
# 函数用途: 只从 task.attributes 中读取工具证据要求；普通 acceptance_checks 文案不会触发机器门禁。
def _required_tool_evidence(task: SubAgentTask) -> set[str]:
    attrs = getattr(task, "attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    return {
        _canonical_tool_name(item)
        for value in (
            attrs.get("required_tool_evidence"),
            attrs.get("acceptance_required_tools"),
            attrs.get("required_tools"),
        )
        for item in _string_list(value)
        if _canonical_tool_name(item)
    }


# LLM: _canonical_tool_name maps exact tool ids and aliases without tokenizing prose.
# 函数用途: 把结构化工具别名归一到 read_file/write_file。
def _canonical_tool_name(value: object) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "read_file": "read_file",
        "file_read": "read_file",
        "file_content": "read_file",
        "write_file": "write_file",
        "file_write": "write_file",
        "file_written": "write_file",
    }
    return aliases.get(text, "")


# LLM: _string_list normalizes machine list fields only.
# 函数用途: 兼容字符串或列表配置，不拆分一句普通自然语言。
def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]
