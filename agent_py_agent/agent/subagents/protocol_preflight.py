
from __future__ import annotations

"""Tool preflight for subagent task envelopes."""

from dataclasses import dataclass, field

from .protocol import ProtocolIssue, TaskEnvelope


@dataclass(frozen=True)
class ToolPreflightResult:

    ok: bool
    issues: list[ProtocolIssue] = field(default_factory=list)
    effective_tools: list[str] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [item.to_dict() for item in self.issues],
            "effective_tools": list(self.effective_tools),
            "reserved": dict(self.reserved),
        }


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


def _effective_tools(allowed: list[str], available: list[str]) -> list[str]:
    if not allowed:
        return available
    allowed_set = set(allowed)
    return [tool for tool in available if tool in allowed_set or tool in {"read_file", "write_file", "list_files", "search"}]


def _tool_issue(
    code: str,
    field: str,
    message: str,
    reserved: dict[str, object] | None = None,
) -> ProtocolIssue:
    return ProtocolIssue(kind="ToolContractError", code=code, field=field, message=message, reserved=reserved or {})


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = ["ToolPreflightResult", "run_tool_preflight"]
