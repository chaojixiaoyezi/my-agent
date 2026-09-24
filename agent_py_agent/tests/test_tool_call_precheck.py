"""审批前与批准后执行前的工具可用性复核：只对声明 precheck_availability 的代理生效，失效按 TOOL_UNAVAILABLE 提前拦下，不进入执行器、不写审批事件。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.round_execution import _with_applied_approval_fact
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.tooling.models import BaseTool, ToolAvailability, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class _ProxyLike(BaseTool):
    """LLM: 模拟插件/MCP 代理：可切换的复核结果、可选抛异常，计数证明复核与执行是否发生。"""

    model_spec = make_test_model_spec("proxy_tool", category="plugins", description="test-only proxy")
    runtime_policy = make_test_runtime_policy("dangerous")

    def __init__(self, *, effect: str = "dangerous") -> None:
        self.runtime_policy = make_test_runtime_policy(effect)
        self.precheck_ready = True
        self.precheck_error: Exception | None = None
        self.executions = self.availability_calls = self.precheck_calls = 0

    def availability(self) -> ToolAvailability:
        self.availability_calls += 1
        return ToolAvailability.ready()

    def precheck_availability(self) -> ToolAvailability:
        self.precheck_calls += 1
        if self.precheck_error is not None:
            raise self.precheck_error
        if self.precheck_ready:
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("原插件已停用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")

    def execute(self, params) -> ToolHandlerOutcome:
        _ = params
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "executed")


class _Builtin(BaseTool):
    """LLM: 没有 precheck 的内置样式工具；availability 计数证明复核从不再调它。"""

    model_spec = make_test_model_spec("builtin_tool", description="test-only builtin")
    runtime_policy = make_test_runtime_policy("dangerous")

    def __init__(self) -> None:
        self.availability_calls = self.executions = 0

    def availability(self) -> ToolAvailability:
        self.availability_calls += 1
        return ToolAvailability.ready()

    def execute(self, params) -> ToolHandlerOutcome:
        _ = params
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, "executed")


def _run(tmp_path, tool, *, write_boundary=None):
    return execute_canonical_test_call(tmp_path, tools={tool.model_spec.name: tool}, tool_name=tool.model_spec.name,
                                       arguments={}, write_boundary=write_boundary)


def _approved_boundary(first):
    binding = {**dict(first.decision.approval_request), "approval_id": "approval-test-call", "status": "APPROVED"}
    return {"approved_actions": [binding]}


def test_pre_approval_precheck_denies_unavailable_proxy_without_asking(tmp_path):
    tool = _ProxyLike()
    tool.precheck_ready = False
    execution = _run(tmp_path, tool)
    assert execution.decision.status == "deny" and execution.decision.reason_codes == ("TOOL_UNAVAILABLE",)
    assert execution.decision.approval_request is None
    assert execution.decision.evidence["precheck"] == "pre_approval" and execution.decision.evidence["failure_stage"] == "runtime_gate"
    result = execution.result
    assert result.status == "failed" and result.error_code == "TOOL_UNAVAILABLE"
    assert result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
    assert result.handler_executed is False and result.applied_approval is None
    assert "approval_pending" not in execution.states and "failed" in execution.states
    assert "审批前" in result.render_for_prompt() and "原插件已停用" in result.render_for_prompt()
    assert tool.executions == 0 and tool.precheck_calls == 1


def test_post_approval_precheck_denies_before_execution(tmp_path):
    tool = _ProxyLike()
    first = _run(tmp_path, tool)
    assert first.result.status == "approval_required" and first.decision.approval_request
    tool.precheck_ready = False
    second = _run(tmp_path, tool, write_boundary=_approved_boundary(first))
    assert second.decision.status == "deny" and second.decision.evidence["precheck"] == "post_approval"
    assert second.decision.evidence["approval_applied"] is True
    assert second.result.error_code == "TOOL_UNAVAILABLE" and second.result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
    assert second.result.handler_executed is False and tool.executions == 0
    assert "批准后、执行前" in second.result.render_for_prompt()
    assert "running" not in second.states and "failed" in second.states


def test_approved_proxy_executes_when_precheck_passes(tmp_path):
    tool = _ProxyLike()
    first = _run(tmp_path, tool)
    second = _run(tmp_path, tool, write_boundary=_approved_boundary(first))
    assert second.result.status == "succeeded" and tool.executions == 1
    assert second.decision.evidence["approval_applied"] is True
    assert tool.precheck_calls == 2, "审批前一次、批准后执行前一次"


def test_builtin_without_precheck_is_never_rechecked(tmp_path):
    tool = _Builtin()
    first = _run(tmp_path, tool)
    assert first.result.status == "approval_required"
    assert tool.availability_calls == 1, "只有建快照时读过一次"
    second = _run(tmp_path, tool, write_boundary=_approved_boundary(first))
    assert second.result.status == "succeeded" and tool.executions == 1
    assert tool.availability_calls == 2, "第二次同样只是新建快照的那一次，执行链没有再调 availability"


def test_free_call_skips_precheck(tmp_path):
    tool = _ProxyLike(effect="read_only")
    tool.precheck_ready = False
    execution = _run(tmp_path, tool)
    assert execution.result.status == "succeeded" and tool.executions == 1 and tool.precheck_calls == 0


def test_precheck_exception_counts_as_unavailable(tmp_path):
    tool = _ProxyLike()
    tool.precheck_error = RuntimeError("boom")
    execution = _run(tmp_path, tool)
    assert execution.decision.status == "deny" and execution.result.reported_error_code == "TOOL_UNAVAILABLE"
    assert execution.decision.evidence["reason"] == "precheck failed: RuntimeError" and tool.executions == 0


def test_post_approval_denial_does_not_carry_applied_approval_fact(tmp_path):
    tool = _ProxyLike()
    first = _run(tmp_path, tool)
    tool.precheck_ready = False
    denied = _run(tmp_path, tool, write_boundary=_approved_boundary(first))
    resumed = _with_applied_approval_fact(denied, approval_request=SimpleNamespace(permission_id="perm-1"),
                                          decision=ToolApprovalDecision("perm-1", "approved"))
    assert resumed.result.applied_approval is None
    ok = _run(tmp_path, _ProxyLike(), write_boundary=_approved_boundary(first))
    tagged = _with_applied_approval_fact(ok, approval_request=SimpleNamespace(permission_id="perm-1"),
                                         decision=ToolApprovalDecision("perm-1", "approved"))
    assert tagged.result.applied_approval is not None and tagged.result.applied_approval.decision == "approved"


@pytest.mark.parametrize("code", ["PLUGIN_ACTIVATION_UNAVAILABLE", "MCP_CONNECTION_CLOSED"])
def test_precheck_reason_codes_are_registered_as_non_retryable(code):
    contract = error_contract(code)
    assert contract.code == code and contract.retryable is False
    assert contract.recommended_action == error_contract("TOOL_UNAVAILABLE").recommended_action
