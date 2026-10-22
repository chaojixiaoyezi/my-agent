from __future__ import annotations

"""restart_gateway tool: hosted-only, admin main agent only, returns immediately with a scheduled request."""

import json
import os
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import gateway_paths, read_json_file
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.gateway_restart_tool import RestartGatewayTool


def _agent(root: Path, **overrides) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", gateway_workspace="gateway", **overrides), root)


def test_refuses_outside_gateway_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv(service.HOSTING_GATEWAY_PID_ENV, raising=False)
    agent = _agent(tmp_path)
    outcome = RestartGatewayTool(agent).execute({"reason": "测试"})
    assert (outcome.ok, outcome.error_code, outcome.effect_outcome) == (False, "GATEWAY_RESTART_NOT_HOSTED", "not_started")
    assert not service.restart_request_path(gateway_paths(agent)).exists()


def test_schedules_request_for_hosting_gateway_with_requester_facts(tmp_path, monkeypatch):
    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    agent = _agent(tmp_path)
    outcome = RestartGatewayTool(agent).execute({"reason": "加载新的通道配置"})
    assert outcome.ok is True
    body = json.loads(outcome.output)
    assert body["status"] == "scheduled"
    assert body["target_pid"] == os.getpid()
    assert "不要再次调用 restart_gateway" in body["hint"]
    request = read_json_file(service.restart_request_path(gateway_paths(agent)))
    assert request["request_id"] == body["request_id"]
    requester = request["requester"]
    assert requester["kind"] == "agent_tool"
    assert requester["conversation_store_root"] == str(agent.conversation_store.storage.root)


def test_missing_reason_and_cooldown_are_not_started(tmp_path, monkeypatch):
    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    agent = _agent(tmp_path)
    tool = RestartGatewayTool(agent)
    missing = tool.execute({"reason": "  "})
    assert (missing.ok, missing.error_code) == (False, "TOOL_INVALID_ARGUMENTS")
    first = json.loads(tool.execute({"reason": "r"}).output)
    paths = gateway_paths(agent)
    service.mark_restart_drained(paths, read_json_file(service.restart_request_path(paths)),
                                 old_pid=os.getpid(), old_process_started_at=0.0, active_turns_at_exit=0)
    cooled = tool.execute({"reason": "again"})
    assert (cooled.ok, cooled.error_code, cooled.effect_outcome) == (False, "GATEWAY_RESTART_COOLDOWN", "not_started")
    assert json.loads(cooled.output)["status"] == "cooldown"
    assert first["status"] == "scheduled"


def test_registered_only_for_admin_main_agent_when_enabled(tmp_path):
    enabled = _agent(tmp_path / "on")
    assert enabled.tools.owner_type == "main_agent"
    assert "restart_gateway" in enabled.tools.tools
    disabled = _agent(tmp_path / "off", enable_gateway_restart_tool=False)
    assert "restart_gateway" not in disabled.tools.tools


def test_trusted_admin_restart_requires_no_additional_confirmation(tmp_path):
    """可信管理员已授权的正常重启免逐次审批，handler 仍单独守身份与排空。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    tool = SimpleNamespace(model_spec=RestartGatewayTool.model_spec, runtime_policy=RestartGatewayTool.runtime_policy)
    snapshot = runtime_snapshot_for_tools({"restart_gateway": tool})
    call = canonical_test_call(snapshot, "restart_gateway", {"reason": "用户要求重启"})

    for mode in ("ask", "auto"):
        decision = ActionPolicy().decide(ActionPolicyRequest(call, snapshot, tmp_path, approval_mode=mode))
        assert decision.status == "allow", mode


def test_direct_handler_rejects_ordinary_group_and_child_without_writing(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    for kind in ('user', 'group'):
        agent = _agent(tmp_path/kind, my_agent_owner_provider='feishu', my_agent_owner_kind=kind,
                       my_agent_owner_id='synthetic-owner')
        tool = RestartGatewayTool(agent)
        assert not tool.availability().available
        outcome = tool.execute({'reason':'不能借宿主工具'})
        assert not outcome.ok and outcome.error_code == 'TOOL_PERMISSION_DENIED'
        assert not service.restart_request_path(gateway_paths(agent)).exists()
    admin = _agent(tmp_path/'admin')
    old = set_current_subagent_context(admin, run_id='synthetic-child')
    try:
        outcome = RestartGatewayTool(admin).execute({'reason':'子代理不能重启'})
        assert not outcome.ok and outcome.error_code == 'TOOL_PERMISSION_DENIED'
    finally:
        restore_current_subagent_context(admin, old)
    assert not service.restart_request_path(gateway_paths(admin)).exists()


# LLM: 经canonical执行器核实重启请求不占真实工具门、不写tool_operations；仅在临时目录写请求，不重启。
# 函数用途: 验证管理员重启控制工具绕过副作用排空计数与操作账，但确实提交请求。
def test_incident_restart_control_bypasses_tool_gate_and_operation_ledger(tmp_path, monkeypatch):
    from agent_py_agent.agent.concurrency import restart_gate
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling import gateway_restart_tool as module
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    monkeypatch.setenv(service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    agent = _agent(tmp_path)
    store = LocalStore(tmp_path / 'operations.db', enable_fts=False)
    observed = []
    original = module.submit_restart_request
    # LLM: 包装原请求写者只记录入口工具计数，不替换其持久请求逻辑。
    # 函数用途: 检查handler执行期间也没有计入待排空工具。
    def submit(*args, **kwargs):
        observed.append(restart_gate.executing_tool_count())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'submit_restart_request', submit)
    outcome = execute_canonical_test_call(tmp_path, tools={'restart_gateway': RestartGatewayTool(agent)},
                                        tool_name='restart_gateway', arguments={'reason': '定时维护'},
                                        operation_store=store, operation_store_required=True).result
    assert outcome.ok, outcome.content
    assert observed == [0], 'restart工具不能占用被自己排空的工具计数'
    assert store.list_tool_operations(owner_id='test-owner') == []
    assert service.pending_restart_request(gateway_paths(agent), pid=os.getpid()) is not None
