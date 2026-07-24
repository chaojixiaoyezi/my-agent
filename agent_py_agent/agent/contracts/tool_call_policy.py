from __future__ import annotations

"""LLM: 定义工具名授权与 canonical 输入 Schema 的纯函数合同，不执行工具或修改运行状态。

模块用途: 在统一工具执行入口复核工具范围、完整参数结构和显式阻断模式，返回可审计决策。
"""

import re
from dataclasses import dataclass, field
from typing import Any

from .recovery import RecoveryAction, RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload
from .tool_input_schema import validate_tool_input
from .tool_protocol_v2 import normalize_tool_call


# LLM: input_schemas 是参数约束的唯一 policy 字段；不得重新加入 required/type 拍平副本。
# 类用途: 描述某次工具调用允许看到哪些工具，以及每个工具必须满足的完整输入结构。
@dataclass(frozen=True)
class ToolCallPolicy:
    available_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    input_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked_argument_patterns: tuple[str, ...] = ()


# LLM: issues 保存 Schema 关键字和 JSON 路径，不保存原始参数值，避免恢复证据泄露输入正文。
# 类用途: 表示一次工具调用能否继续，以及模型应修正哪些字段或结构。
@dataclass(frozen=True)
class ToolCallPolicyDecision:
    ok: bool
    tool_name: str
    error_code: str = ""
    findings: tuple[str, ...] = ()
    issues: tuple[dict[str, Any], ...] = ()

    # LLM: recovery 投影必须沿用结构化 code/issues，不能从展示文案反推错误类别。
    # 函数用途: 把策略结果转换成可写入运行门和恢复合同的字典。
    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "error_code": self.error_code,
            "findings": list(self.findings),
            "issues": [dict(item) for item in self.issues],
        }
        recovery = recovery_envelope_from_gate_payload(
            RecoveryEnvelopeRequest(
                gate="tool_call_policy",
                status="ALLOW" if self.ok else "NEED_REPAIR",
                allowed=self.ok,
                findings=_finding_payloads(self),
                recommended_action=(
                    RecoveryAction.CONTINUE.value
                    if self.ok
                    else RecoveryAction.REPAIR_TOOL_CALL.value
                ),
                evidence={"tool_name": self.tool_name} if self.tool_name else {},
            )
        )
        if recovery is not None:
            payload["recovery"] = recovery.to_dict()
        return payload


# LLM: 校验顺序固定为名称范围、完整 Schema、显式阻断模式；调用方必须在副作用前使用结果。
# 函数用途: 纯函数检查一个规范 ToolCall 是否符合当前工具调用策略。
def validate_tool_call_policy(payload: Any, policy: ToolCallPolicy) -> ToolCallPolicyDecision:
    call = normalize_tool_call(payload)
    tool_name = call.tool_name
    if not tool_name:
        return _decision(False, tool_name, "TOOL_NAME_REQUIRED")
    available = _name_set(policy.available_tools)
    if available and tool_name not in available:
        return _decision(False, tool_name, "TOOL_NOT_FOUND")
    if tool_name in _name_set(policy.denied_tools):
        return _decision(False, tool_name, "TOOL_DENIED")
    allowed = _name_set(policy.allowed_tools)
    if allowed and tool_name not in allowed:
        return _decision(False, tool_name, "TOOL_NOT_ALLOWED")

    schema = policy.input_schemas.get(tool_name)
    if isinstance(schema, dict):
        validation = validate_tool_input(call.input, schema)
        if not validation.ok:
            issues = tuple(item.to_dict() for item in validation.issues)
            return _decision(
                False,
                tool_name,
                validation.primary_error_code,
                validation.fields,
                issues,
            )
    blocked = _blocked_argument_matches(call.input, policy.blocked_argument_patterns)
    if blocked:
        return _decision(False, tool_name, "TOOL_PARAMETER_BLOCKED", blocked)
    return _decision(True, tool_name, "")


# LLM: 阻断表达式只检查字符串叶子并返回路径，不把匹配值写入证据。
# 函数用途: 找出命中管理员显式危险模式的参数位置。
def _blocked_argument_matches(
    params: dict[str, Any],
    patterns: tuple[str, ...],
) -> tuple[str, ...]:
    if not patterns:
        return ()
    findings: list[str] = []
    for path, value in _string_values(params):
        if any(re.search(pattern, value) for pattern in patterns):
            findings.append(path)
    return tuple(findings)


# LLM: 递归只产出路径和值供本地模式匹配，调用方不得把值写入审计。
# 函数用途: 展开对象和数组中的所有字符串参数。
def _string_values(value: Any, prefix: str = "") -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((prefix or "$", value),)
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows.extend(_string_values(item, f"{prefix}.{key}" if prefix else str(key)))
        return tuple(rows)
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_string_values(item, f"{prefix}[{index}]"))
        return tuple(rows)
    return ()


# LLM: 工具集合统一去空白但不做大小写或别名兼容，避免隐式扩权。
# 函数用途: 把策略中的工具名元组转换成精确匹配集合。
def _name_set(names: tuple[str, ...]) -> set[str]:
    return {str(item).strip() for item in names if str(item).strip()}


# LLM: decision helper 不加工 code/path/issue，保持下层验证事实原样可追踪。
# 函数用途: 简化不可变策略结果的创建。
def _decision(
    ok: bool,
    tool_name: str,
    error_code: str,
    findings: tuple[str, ...] = (),
    issues: tuple[dict[str, Any], ...] = (),
) -> ToolCallPolicyDecision:
    return ToolCallPolicyDecision(
        ok=ok,
        tool_name=tool_name,
        error_code=error_code,
        findings=findings,
        issues=issues,
    )


# LLM: recovery finding 只包含安全路径和约束元数据，不包含用户参数原值。
# 函数用途: 为统一恢复合同构造一条参数错误证据。
def _finding_payloads(decision: ToolCallPolicyDecision) -> tuple[dict[str, object], ...]:
    if not decision.error_code:
        return ()
    evidence: dict[str, object] = {}
    if decision.findings:
        evidence["fields"] = list(decision.findings)
    if decision.issues:
        evidence["issues"] = [dict(item) for item in decision.issues]
    return ({"code": decision.error_code, "evidence": evidence},)


__all__ = ["ToolCallPolicy", "ToolCallPolicyDecision", "validate_tool_call_policy"]
