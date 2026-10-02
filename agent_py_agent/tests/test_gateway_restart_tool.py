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


def test_restart_always_asks_even_in_autonomous_or_full_access_mode(tmp_path):
    """3a 2026-10-02：重启影响所有会话，任何审批模式都弹确认（完全放行在审批层也是 auto）；也堵上 S1 的重启刷新快照路径。"""
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
        assert decision.status == "ask", mode
