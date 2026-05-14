# LLM: Diagnostic acceptance helpers keep QA/tester semantics separate from normal worker acceptance.
# 模块用途: 识别测试/找茬角色，并把“成功发现问题”的输出转成非阻塞验收事实。

from __future__ import annotations

"""diagnostic-role acceptance review helpers."""

from dataclasses import replace

from .models import SubAgentTask
from .reports import AcceptanceReviewFinding

_DIAGNOSTIC_COMPLETION_STATUSES = frozenset({
    "completed_with_issues",
    "completed_with_findings",
    "issues_found",
    "findings_found",
})
_DIAGNOSTIC_NON_BLOCKING_FINDINGS = frozenset({
    "tests_passed",
    "no_unresolved_patches",
})
_DIAGNOSTIC_ROLES = frozenset({
    "tester",
    "test",
    "qa_tester",
    "bug_finder",
    "bugfinder",
    "bug-finder",
})


# LLM: diagnostic_review_inputs keeps QA-finding roles from being marked failed for reporting bugs.
# 函数用途: tester/bug_finder 输出 COMPLETED_WITH_ISSUES 时，把“发现的产品问题”降为 P2 事实。
def diagnostic_review_inputs(task: SubAgentTask, inputs):
    if not _is_completed_diagnostic_role(task, inputs.output):
        return inputs
    findings = [_diagnostic_finding(item) for item in inputs.findings]
    return replace(inputs, findings=findings)


# LLM: _diagnostic_finding downgrades only product-issue findings, never channel/readiness/capability gates.
# 函数用途: 保留 finding.ok=False 作为“发现问题”的证据，只把 severity 调成 P2。
def _diagnostic_finding(finding: AcceptanceReviewFinding) -> AcceptanceReviewFinding:
    if finding.ok or finding.severity == "P2" or finding.name not in _DIAGNOSTIC_NON_BLOCKING_FINDINGS:
        return finding
    message = f"{finding.message}（诊断角色已报告，交由父级 repair/acceptor 闭环。）"
    return replace(finding, severity="P2", message=message)


# LLM: _is_completed_diagnostic_role requires both a QA-like role and completed-with-issues status.
# 函数用途: 判断 tester/bug_finder 是否已经完成“找问题”职责，避免 worker 借状态绕过失败。
def _is_completed_diagnostic_role(task: SubAgentTask, output: dict) -> bool:
    if not _is_diagnostic_role(task):
        return False
    return _normalized_output_status(output) in _DIAGNOSTIC_COMPLETION_STATUSES


# LLM: _is_diagnostic_role centralizes lightweight role matching without importing role-template machinery.
# 函数用途: 识别测试/找茬角色；只匹配诊断角色，不把 acceptor 或普通 worker 放宽。
def _is_diagnostic_role(task: SubAgentTask) -> bool:
    role = _normalized_role_text(getattr(task, "role", ""))
    name = str(getattr(task, "agent_name", "") or "").lower()
    return role in _DIAGNOSTIC_ROLES or role.endswith("_tester") or "测试" in name or "找茬" in name


# LLM: _normalized_output_status reads status from modern or legacy output shapes.
# 函数用途: 统一 structured_output.status / output.status 的大小写和分隔符。
def _normalized_output_status(output: dict) -> str:
    structured = output.get("structured_output")
    value = structured.get("status") if isinstance(structured, dict) else output.get("status")
    return _normalized_role_text(value)


# LLM: _normalized_role_text is intentionally tiny because acceptance matching must stay deterministic.
# 函数用途: 把角色/状态里的大小写、空格和横线归一，降低模型输出轻微漂移带来的误判。
def _normalized_role_text(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
