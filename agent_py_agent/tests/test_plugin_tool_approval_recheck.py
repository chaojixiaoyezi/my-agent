"""C12e 集成验收：可执行插件工具的审批前复核与批准后复核（真实插件进程 + 真实安装表激活 + 真实执行器审批流）。

来源：G05 恢复边界（2026-09-28）只用内容包验证了“审批等待跨过停用之后本轮不能再用旧代”，可执行插件工具的审批前和批准后
复核标为“另行安排”。宿主的复核机制早已在位（executor._precheck_before_approval / 批准后复核，PluginProxyTool 用固定激活引用
鲜活读安装表），但已有用例只用假代理。这里把它接到真实链路的三件事上：托管 MCP 进程（managed_client 起真进程）、
PluginInstallStore 里真实的激活与撤销、ToolExecutor 的真实审批裁决；插件每收到一次业务调用就往文件里记一行，用来证明
被拒的调用根本没有发出去。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_plugin_tool_approval_recheck.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.plugin_runtime import PluginProxyTool
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _config
from agent_py_agent.tests.test_plugin_activation import revocation
from agent_py_agent.tests.test_plugin_activation_ref import plugin_reference

TOOL = "plugin__c12e_probe__echo"


@pytest.fixture
def plugin(tmp_path):
    """真实托管插件进程（同 test_plugin_mcp_transport.managed_client 的组装）；每次业务调用记一行到 calls 文件。
    产品里激活引用由 PluginMCPClient 以 activation_ref 持有，这里把同一份真实引用挂到客户端上。"""
    calls = tmp_path / "calls"
    script = _ECHO_SERVER.replace(
        '            params = req.get("params") or {}',
        f'            open({str(calls)!r}, "a").write("called\\n")\n            params = req.get("params") or {{}}')
    store, entry, reference, _owner = plugin_reference(tmp_path, phase="active")
    client = MCPStdioClient(_config(script, cwd=str(tmp_path)), activation=reference)
    client.activation_ref = reference
    client.start()
    try:
        yield client, store, entry, calls
    finally:
        client.stop()


def _proxy(client, effect: str) -> PluginProxyTool:
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    spec = make_test_model_spec(TOOL, input_schema=schema, category="plugins", description="test-only executable plugin echo")
    return PluginProxyTool(client, "echo", spec, make_test_runtime_policy(effect))


def _call(root: Path, tool: PluginProxyTool, write_boundary=None):
    """每次调用现建快照：等同“下一轮”，可用性在快照里重新取。"""
    return execute_canonical_test_call(root, tools={TOOL: tool}, tool_name=TOOL, arguments={"text": "hi"},
                                       write_boundary=write_boundary)


def _call_in_turn(root: Path, snapshot, write_boundary=None):
    """沿用本轮开头冻结的快照：同一轮里插件中途被停用时，快照仍写着可用，只能靠审批前/批准后/发送前复核拦下。"""
    call = canonical_test_call(snapshot, TOOL, {"text": "hi"}, call_id="c12e-call")
    return ToolExecutor().execute(ToolExecutorRequest(
        call=call, runtime_snapshot=snapshot, workspace_root=root, workspace_roots=(root,), write_boundary=write_boundary,
    ))


def _approved(first):
    return {"approved_actions": [{**dict(first.decision.approval_request), "approval_id": "approval-c12e", "status": "APPROVED"}]}


def _call_count(calls: Path) -> int:
    return len(calls.read_text().splitlines()) if calls.exists() else 0


def _refused_by_recheck(execution, calls: Path, precheck: str) -> None:
    """同轮停用：快照仍写着可用，由执行器在指定复核点鲜活核对原激活拒绝；不进 handler、插件进程没收到调用。"""
    assert execution.decision.status == "deny" and execution.decision.reason_codes == ("TOOL_UNAVAILABLE",)
    assert execution.decision.evidence["precheck"] == precheck
    assert execution.result.error_code == "TOOL_UNAVAILABLE"
    assert execution.result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
    assert execution.result.handler_executed is False
    assert _call_count(calls) == 0


def test_disabled_while_waiting_for_approval_is_refused_after_approval(tmp_path, plugin):
    client, store, entry, calls = plugin
    snapshot = runtime_snapshot_for_tools({TOOL: _proxy(client, "dangerous")})
    first = _call_in_turn(tmp_path, snapshot)
    assert first.result.status == "approval_required" and _call_count(calls) == 0

    # 审批等待期间管理员停用插件（真实安装表撤销激活），用户随后迟到批准。
    store.change_activation(revocation(entry))
    second = _call_in_turn(tmp_path, snapshot, write_boundary=_approved(first))

    _refused_by_recheck(second, calls, "post_approval")
    assert second.decision.evidence["approval_applied"] is True


def test_disabled_in_the_same_turn_is_refused_without_asking(tmp_path, plugin):
    client, store, entry, calls = plugin
    snapshot = runtime_snapshot_for_tools({TOOL: _proxy(client, "dangerous")})
    store.change_activation(revocation(entry))

    execution = _call_in_turn(tmp_path, snapshot)

    _refused_by_recheck(execution, calls, "pre_approval")
    assert execution.decision.approval_request is None, "插件已停用就不弹审批"


def test_free_call_in_the_same_turn_is_not_started(tmp_path, plugin):
    """免审批的只读工具不走审批复核，发送前在 handler 里核对原激活：报 TOOL_UNAVAILABLE、not_started，插件没收到调用。"""
    client, store, entry, calls = plugin
    snapshot = runtime_snapshot_for_tools({TOOL: _proxy(client, "read_only")})
    store.change_activation(revocation(entry))

    execution = _call_in_turn(tmp_path, snapshot)

    assert execution.result.error_code == "TOOL_UNAVAILABLE"
    assert execution.result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
    assert execution.result.effect_outcome == "not_started"
    assert _call_count(calls) == 0


def test_next_turn_after_disable_is_refused_at_the_runtime_gate(tmp_path, plugin):
    """下一轮现建快照时可用性已是不可用，运行时门直接拒绝，不弹审批。"""
    client, store, entry, calls = plugin
    store.change_activation(revocation(entry))

    execution = _call(tmp_path, _proxy(client, "dangerous"))

    assert execution.decision.status == "deny" and execution.decision.evidence.get("failure_stage") == "runtime_gate"
    assert "precheck" not in execution.decision.evidence, "新快照本身已标不可用，轮不到审批复核"
    assert execution.decision.approval_request is None and execution.result.handler_executed is False
    assert _call_count(calls) == 0


def test_approved_call_runs_while_the_plugin_stays_enabled(tmp_path, plugin):
    client, _store, _entry, calls = plugin
    tool = _proxy(client, "dangerous")
    first = _call(tmp_path, tool)

    second = _call(tmp_path, tool, write_boundary=_approved(first))

    assert second.result.status == "succeeded" and second.decision.evidence["approval_applied"] is True
    assert _call_count(calls) == 1
