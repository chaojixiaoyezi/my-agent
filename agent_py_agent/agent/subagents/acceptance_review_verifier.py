# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""deterministic verifier checks for acceptance review records."""

from .models import SubAgentTask
from .reports import AcceptanceReviewFinding
from .services.acceptance_machine_evidence import passed_test_execution_report


# LLM: build_verifier_checks 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建verifier检查所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def build_verifier_checks(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    """Verify evidence packets and parent findings before acceptance."""
    packet_ids = {item.id for item in task.evidence_packets if item.id}
    packets_with_refs = [
        item for item in task.evidence_packets if item.evidence_refs or item.artifact_refs
    ]
    machine_report = None if task.evidence_packets else passed_test_execution_report(task)
    unresolved_risks = [
        risk
        for packet in task.evidence_packets
        for risk in packet.unresolved_risks
        if str(risk).strip()
    ]
    findings_without_chain = [
        item
        for item in task.findings
        if not item.evidence_refs
        and not any(packet_id in packet_ids for packet_id in item.evidence_packet_ids)
    ]
    return [
        _verifier_packets_finding(task, packets_with_refs, machine_report, created_at),
        _verifier_findings_finding(task, findings_without_chain, created_at),
        _verifier_risks_finding(task, unresolved_risks, created_at),
    ]


# LLM: _verifier_packets_finding 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理verifierpacketsfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _verifier_packets_finding(task, packets_with_refs, machine_report, created_at):
    ok = (len(packets_with_refs) == len(task.evidence_packets) and bool(packets_with_refs)) or (
        not task.evidence_packets and machine_report is not None
    )
    return AcceptanceReviewFinding(
        name="verifier_evidence_packets_traceable",
        ok=ok,
        severity="P1",
        message=_verifier_packets_message(ok, machine_report),
        evidence_path=str(machine_report.json_path) if machine_report is not None else task.output_json,
        created_at=created_at,
    )


# LLM: _verifier_packets_message distinguishes worker packet traceability from machine-test traceability.
# 函数用途: 生成 verifier 文案；无 worker packet 时通过父级真实测试报告解释通过来源。
def _verifier_packets_message(ok, machine_report):
    if ok and machine_report is not None:
        return "verifier 确认父级真实测试报告可作为机器证据链。"
    if ok:
        return "verifier 确认 evidence packets 均有 refs。"
    return "verifier 发现存在缺少 refs 的 evidence packet。"


# LLM: _verifier_findings_finding 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理verifierfindingsfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _verifier_findings_finding(task, findings_without_chain, created_at):
    return AcceptanceReviewFinding(
        name="verifier_findings_cite_evidence",
        ok=not findings_without_chain,
        severity="P1" if findings_without_chain else "P2",
        message="verifier 确认 findings 引用了 evidence。"
        if not findings_without_chain
        else f"verifier 发现 {len(findings_without_chain)} 条 finding 缺少 evidence 引用。",
        evidence_path=task.output_json,
        created_at=created_at,
    )


# LLM: _verifier_risks_finding 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理verifierrisksfinding相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _verifier_risks_finding(task, unresolved_risks, created_at):
    return AcceptanceReviewFinding(
        name="verifier_no_unresolved_evidence_risks",
        ok=not unresolved_risks,
        severity="P1",
        message="verifier 未发现未解决 evidence risk。"
        if not unresolved_risks else f"verifier 发现未解决风险: {unresolved_risks[0]}",
        evidence_path=task.output_json,
        created_at=created_at,
    )
