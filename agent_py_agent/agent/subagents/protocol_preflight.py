# LLM: Tool preflight checks a task envelope before runner execution without removing basic tools.
# 模块用途: 在子代理开工前检查工具和写入合同，返回结构化问题而不是让模型跑到一半才失败。

from __future__ import annotations

"""Tool preflight for subagent task envelopes."""

from dataclasses import dataclass, field

from .protocol import ProtocolIssue, TaskEnvelope


# LLM: ToolPreflightResult is a non-mutating readiness report for a task envelope.
# 类用途: 汇总工具/路径预检结果；它不发权限、不改任务，只告诉父级缺什么。
@dataclass(frozen=True)
class ToolPreflightResult:

    ok: bool
    issues: list[ProtocolIssue] = field(default_factory=list)
    effective_tools: list[str] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)

    # LLM: to_dict serializes the preflight report for board/recovery/event logs.
    # 函数用途: 输出 JSON 友好字段，保留每条结构化 issue。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [item.to_dict() for item in self.issues],
            "effective_tools": list(self.effective_tools),
            "reserved": dict(self.reserved),
        }


# LLM: run_tool_preflight validates explicit tool and write contracts before execution.
# 函数用途: 检查工具是否存在、写入根是否明确、controlled_exec 是否已有授权；不会剥夺基础读写工具。
def run_tool_preflight(
    envelope: TaskEnvelope,
    *,
    available_tools: set[str] | list[str] | tuple[str, ...],
) -> ToolPreflightResult:
    available = _ordered_unique(sorted(str(item) for item in available_tools))
    allowed = _ordered_unique([str(item) for item in envelope.tool_contract.get("allowed_tools", [])])
    issues = [
        *_missing_tool_issues(allowed, set(available)),
        *_write_contract_issues(envelope),
        *_controlled_exec_issues(envelope),
    ]
    return ToolPreflightResult(ok=not issues, issues=issues, effective_tools=_effective_tools(allowed, available))


# LLM: _missing_tool_issues reports explicit missing tools as ToolContractError.
# 函数用途: allowed_tools 显式要求但当前工具集没有时，返回能力缺口而不是静默忽略。
def _missing_tool_issues(allowed: list[str], available: set[str]) -> list[ProtocolIssue]:
    return [
        _tool_issue(
            "missing_allowed_tool",
            "tool_contract.allowed_tools",
            f"allowed tool is unavailable: {tool}",
            reserved={"tool": tool},
        )
        for tool in allowed
        if tool not in available
    ]


# LLM: _write_contract_issues ensures implementation-style tasks know where they may write.
# 函数用途: 有验收输出但没有 allowed_write_roots 时，开工前明确提示父级补路径。
def _write_contract_issues(envelope: TaskEnvelope) -> list[ProtocolIssue]:
    checks = list(envelope.acceptance.get("checks") or [])
    roots = list(envelope.write_contract.get("product_write_roots") or [])
    if checks and not roots:
        return [
            _tool_issue(
                "missing_allowed_write_roots",
                "write_contract.product_write_roots",
                "task has acceptance checks but no product write roots.",
            )
        ]
    return []


# LLM: _controlled_exec_issues checks grants without deciding whether shell should be allowed.
# 函数用途: controlled_exec 显式在工具合同中出现时，需要 grant id 才算可执行。
def _controlled_exec_issues(envelope: TaskEnvelope) -> list[ProtocolIssue]:
    allowed = {str(item) for item in envelope.tool_contract.get("allowed_tools", []) or []}
    grants = list(envelope.tool_contract.get("controlled_exec_grant_ids", []) or [])
    if "controlled_exec" in allowed and not grants:
        return [
            _tool_issue(
                "controlled_exec_grant_missing",
                "tool_contract.controlled_exec_grant_ids",
                "controlled_exec requires a scoped capability grant.",
            )
        ]
    return []


# LLM: _effective_tools keeps basic readable tools available and drops only unavailable explicit extras.
# 函数用途: 返回当前实际可用工具列表，不能因为预检失败把子代理变成没手没脚。
def _effective_tools(allowed: list[str], available: list[str]) -> list[str]:
    if not allowed:
        return available
    allowed_set = set(allowed)
    return [tool for tool in available if tool in allowed_set or tool in {"read_file", "write_file", "list_files", "search"}]


# LLM: _tool_issue standardizes preflight issue naming.
# 函数用途: 生成 ToolContractError 类型的结构化 issue。
def _tool_issue(
    code: str,
    field: str,
    message: str,
    reserved: dict[str, object] | None = None,
) -> ProtocolIssue:
    return ProtocolIssue(kind="ToolContractError", code=code, field=field, message=message, reserved=reserved or {})


# LLM: _ordered_unique preserves model-facing tool order while dropping duplicates.
# 函数用途: 稳定输出工具列表，避免测试和提示词因 set 顺序漂移。
def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = ["ToolPreflightResult", "run_tool_preflight"]
