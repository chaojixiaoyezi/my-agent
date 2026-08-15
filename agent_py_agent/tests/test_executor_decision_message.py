"""LLM: _decision_message 必须把 validation 拒绝的字段级诊断渲染给模型。

模块用途: 实机教训(test2-req-7 urllib3 复刻)MiniMax-M2.7 调 send_message 57 轮全被
TOOL_PARAMETER_TYPE_INVALID 拦、错误只回错误码不带字段详情,模型盲猜重试零推进。
这里锁死:带 issues 的拒绝必须渲染 名:期望类型(与 recovery_hint 契约一致),且不回显参数值。
"""

from __future__ import annotations

from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import (
    _decision_message,
    _render_validation_issues,
)


def _validation_decision(
    issues: list[dict[str, object]],
    *,
    boundary_error: str = "",
) -> ActionDecision:
    evidence: dict[str, object] = {"failure_stage": "validation", "issues": issues}
    if boundary_error:
        evidence["boundary_error"] = boundary_error
    return ActionDecision("deny", ("TOOL_PARAMETER_TYPE_INVALID",), evidence=evidence)


def test_type_issue_renders_field_path_and_expected_type() -> None:
    decision = _validation_decision(
        [{"keyword": "type", "path": "$.attachments", "expected": "array", "actual_type": "string"}]
    )
    message = _decision_message(decision)
    assert "TOOL_PARAMETER_TYPE_INVALID" in message
    assert "$.attachments" in message
    assert "期望 array" in message
    assert "实际 string" in message


def test_required_issue_renders_missing_field() -> None:
    message = _decision_message(
        _validation_decision([{"keyword": "required", "path": "$.message", "expected": True, "actual_type": "missing"}])
    )
    assert "$.message" in message
    assert "必填缺失" in message


def test_unknown_field_issue_renders_extra_property_rejection() -> None:
    message = _decision_message(
        _validation_decision(
            [{"keyword": "additionalProperties", "path": "$.file", "expected": False, "actual_type": "string"}]
        )
    )
    assert "$.file" in message
    assert "未声明字段" in message


def test_other_keyword_falls_back_to_generic_text() -> None:
    message = _decision_message(_validation_decision([{"keyword": "maxItems", "path": "$.attachments", "expected": 10, "actual_type": "array"}]))
    assert "$.attachments" in message
    assert "maxItems" in message


def test_at_most_four_issues_are_rendered() -> None:
    issues = [
        {"keyword": "type", "path": f"$.field{i}", "expected": "array", "actual_type": "string"}
        for i in range(6)
    ]
    message = _decision_message(_validation_decision(issues))
    assert message.count("期望 array") == 4
    assert "$.field5" not in message


def test_non_dict_issue_entries_are_skipped() -> None:
    decision = _validation_decision(["not-a-dict", {"keyword": "type", "path": "$.attachments", "expected": "array", "actual_type": "string"}])
    message = _decision_message(decision)
    assert "$.attachments" in message
    assert "not-a-dict" not in message


def test_boundary_error_and_issues_both_rendered() -> None:
    decision = _validation_decision(
        [{"keyword": "type", "path": "$.attachments", "expected": "array", "actual_type": "string"}],
        boundary_error="write boundary denied",
    )
    message = _decision_message(decision)
    assert "write boundary denied" in message
    assert "$.attachments" in message


def test_deny_without_issues_keeps_old_shape() -> None:
    decision = ActionDecision("deny", ("TOOL_NOT_IN_RUNTIME_SNAPSHOT",))
    message = _decision_message(decision)
    assert "TOOL_NOT_IN_RUNTIME_SNAPSHOT" in message
    assert "参数问题" not in message


def test_ask_status_keeps_approval_text() -> None:
    message = _decision_message(ActionDecision("ask", ("APPROVAL_REQUIRED",)))
    assert "Approval or explicit user confirmation" in message


def test_render_validation_issues_never_echoes_argument_values() -> None:
    """渲染只允许结构化字段,即使 issue 里混入参数正文也不得回显(防凭据进模型)。"""
    rendered = _render_validation_issues(
        [
            {
                "keyword": "type",
                "path": "$.attachments",
                "expected": "array",
                "actual_type": "string",
                "secret": "sk-real-key-123",
            }
        ]
    )
    assert "sk-real-key" not in rendered
    assert "$.attachments: 期望 array, 实际 string" == rendered
