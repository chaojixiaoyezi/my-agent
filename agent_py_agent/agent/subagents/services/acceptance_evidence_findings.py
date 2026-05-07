# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Evidence-related acceptance finding builders."""

from typing import TYPE_CHECKING

from ..reports import AcceptanceReviewFinding

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: build_evidence_findings 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 构建证据findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def build_evidence_findings(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    findings = _base_evidence_findings(task, created_at)
    acceptance_text = ";".join(task.acceptance_checks).lower()
    if "read_file" in acceptance_text:
        findings.append(_required_read_file_finding(task, created_at))
    if "write_file" in acceptance_text:
        findings.append(_required_write_file_finding(task, created_at))
    return findings


# LLM: _base_evidence_findings 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理基础证据findings相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _base_evidence_findings(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    ok_evidence = [item for item in task.evidence if item.ok]
    bad_evidence = [item for item in task.evidence if not item.ok]
    packets_with_refs = [
        item for item in task.evidence_packets if item.evidence_refs or item.artifact_refs
    ]
    return [
        AcceptanceReviewFinding(
            name="evidence_present",
            ok=bool(ok_evidence),
            severity="P0",
            message=f"已有 {len(ok_evidence)} 条可用验收证据。" if ok_evidence else "缺少可用验收证据。",
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="evidence_chain_present",
            ok=bool(packets_with_refs),
            severity="P0",
            message=(
                f"已有 {len(packets_with_refs)} 条 evidence packet 可追溯。"
                if packets_with_refs
                else "缺少带 evidence/artifact refs 的 evidence packet。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="evidence_not_failed",
            ok=not bad_evidence,
            severity="P1",
            message="没有失败验收证据。" if not bad_evidence else f"存在 {len(bad_evidence)} 条失败证据。",
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
    ]


# LLM: _required_read_file_finding 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验requiredread文件finding需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _required_read_file_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    has_read = "read_file" in task.used_tools and any(
        item.ok
        and (
            item.kind in {"read_file", "file_read", "file_content"}
            or "read_file" in item.command.lower()
            or "read_file" in item.summary.lower()
        )
        for item in task.evidence
    )
    return AcceptanceReviewFinding(
        name="acceptance_requires_read_file",
        ok=has_read,
        severity="P0",
        message=(
            "acceptance_checks 要求 read_file，且已有对应工具和证据。"
            if has_read
            else "acceptance_checks 要求 read_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )


# LLM: _required_write_file_finding 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验requiredwrite文件finding需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _required_write_file_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    has_write = "write_file" in task.used_tools and any(
        item.ok
        and (
            item.kind in {"write_file", "file_write", "file_written"}
            or "write_file" in item.command.lower()
            or "write_file" in item.summary.lower()
            or "写入" in item.summary
        )
        for item in task.evidence
    )
    return AcceptanceReviewFinding(
        name="acceptance_requires_write_file",
        ok=has_write,
        severity="P0",
        message=(
            "acceptance_checks 要求 write_file，且已有对应工具和证据。"
            if has_write
            else "acceptance_checks 要求 write_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )
