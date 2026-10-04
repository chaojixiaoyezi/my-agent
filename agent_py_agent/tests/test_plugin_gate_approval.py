"""B5 插件确认不能被会话缓存、长期授权或等待中的自主模式批准。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalWaitOptions,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation import agent_tool_approval as agent_approval
from agent_py_agent.agent.gateway_parts import permission_bridge
from agent_py_agent.agent.gateway_parts.approval_session import ToolApprovalSessionCache
from agent_py_agent.agent.gateway_parts.stream_approval import (
    StreamApproval,
    StreamApprovalRequestOptions,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.user_space import approval_mode, operation_grants


def gate_request():
    call = ToolCall(call_id="call-gate", tool_name="probe", arguments={}, source_protocol="native",
                    schema_hash="sha256:probe", run_id="run-gate", turn_id="turn-gate", attempt_id="attempt-gate")
    request = build_tool_approval_request(call, request_id="request-gate", round_number=1,
                                          call_index=1, description="待确认", grant_key="probe:mode=write")
    return replace(request, binding={**request.binding, "plugin_gate_ref": '{"gates":["gate-a"]}'})


@pytest.mark.parametrize("cached", [False, True])
def test_gateway_plugin_request_publishes_before_cache_or_owner_grant(tmp_path, monkeypatch, cached):
    from agent_py_agent.agent.gateway_parts import stream_approval

    request = gate_request()
    events, grants = [], []
    cache = ToolApprovalSessionCache()
    if cached:
        cache.approve("scope", f"probe:{request.binding['args_hash']}")
    stream = StreamApproval(tmp_path / "chunks", events.append, lambda: None)
    stream.configure(cache, lambda: "scope", grants.append)
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision",
                        lambda path, req, **kwargs: ToolApprovalDecision(req.permission_id, "denied"))
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True,
                            lambda req: ToolApprovalDecision(req.permission_id, "approved")))
    assert result["decision"] == "denied"
    assert events[0]["kind"] == "permission_requested"
    assert grants == []


@pytest.mark.parametrize("kind", ["main", "subagent"])
@pytest.mark.parametrize("cached", [False, True])
def test_agent_plugin_request_publishes_before_cache_or_owner_grant(monkeypatch, kind, cached):
    request = gate_request()
    published, grants = [], []
    scope = SimpleNamespace(run_id="run-gate", claim_id="claim-gate", agent_kind=kind)
    monkeypatch.setattr(agent_approval, "_request_scope", lambda *args: scope)
    monkeypatch.setattr(operation_grants, "owner_operation_granted", lambda *args: True)
    monkeypatch.setattr(operation_grants, "record_owner_operation_grant", lambda *args, **kw: grants.append(args))
    monkeypatch.setattr(agent_approval, "publish_agent_tool_approval",
                        lambda *args, **kw: published.append(kw["request_value"]) or object())
    monkeypatch.setattr(agent_approval, "wait_for_agent_tool_approval",
                        lambda *args, **kw: ToolApprovalDecision(request.permission_id, "denied"))
    sink = agent_approval.AgentToolApprovalSinkMixin()
    sink.agent = SimpleNamespace(home_paths=object())
    sink.thread_id, sink.task_id = "thread-gate", "run-gate"
    key = f"run-gate:claim-gate:probe:{request.binding['args_hash']}"
    sink._approved_session_keys = {key} if cached else set()
    result = sink.request_permission(request.to_dict())
    assert result["decision"] == "denied"
    assert published == [request]
    assert sink._approved_session_keys == ({key} if cached else set())
    assert grants == []


def test_gateway_wait_plugin_gate_cannot_poll_autonomous_approval(tmp_path, monkeypatch):
    request, token, modes = gate_request(), SimpleNamespace(cancelled=False), []
    monkeypatch.setattr(permission_bridge.time, "sleep", lambda interval: setattr(token, "cancelled", True))
    def provider(req):
        modes.append(req)
        return ToolApprovalDecision(req.permission_id, "approved")
    result = permission_bridge.wait_for_gateway_permission_decision(
        tmp_path / "chunks", request, cancellation_token=token,
        options=ToolApprovalWaitOptions(mode_decision_provider=provider))
    assert result.decision == "cancelled"
    assert modes == []


def test_agent_wait_plugin_gate_cannot_poll_autonomous_approval(tmp_path, monkeypatch):
    request, token, modes = gate_request(), SimpleNamespace(cancelled=False), []
    handle = SimpleNamespace(request=request, path=tmp_path / "pending.json", created_at=0,
                             scope_valid=lambda: True, scope=SimpleNamespace(root_task_id="root"))
    monkeypatch.setattr(agent_approval, "read_json_object_report",
                        lambda *args, **kw: SimpleNamespace(load_error=None, payload={"status": "pending"}))
    monkeypatch.setattr(agent_approval, "_record_decision", lambda *args: None)
    monkeypatch.setattr(agent_approval, "_consumer_seen_at", lambda *args: 0)
    monkeypatch.setattr(agent_approval, "_remove_exact_record", lambda *args: None)
    monkeypatch.setattr(agent_approval.time, "sleep", lambda interval: setattr(token, "cancelled", True))
    def provider(req):
        modes.append(req)
        return ToolApprovalDecision(req.permission_id, "approved")
    result = agent_approval.wait_for_agent_tool_approval(
        handle, cancellation_token=token,
        options=ToolApprovalWaitOptions(discovery_seconds=0, mode_decision_provider=provider))
    assert result.decision == "unavailable"
    assert modes == []


def test_plugin_gate_autonomous_provider_never_reads_owner_grant(monkeypatch):
    modes = []
    monkeypatch.setattr(approval_mode, "owner_operation_granted", lambda *args: modes.append("grant") or True)
    tool = SimpleNamespace(runtime_policy=SimpleNamespace(approval_policy=SimpleNamespace(mode="dangerous")))
    agent = SimpleNamespace(tools=SimpleNamespace(tools={"probe": tool}), home_paths=object())
    assert approval_mode.autonomous_tool_decision(agent, gate_request()) is None
    assert modes == []
