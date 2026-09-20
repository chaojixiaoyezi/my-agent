"""用户显式审批选择、隔离、权限视图和主/子代理等待恢复的合同验证。"""

import json
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_scope import selected_model_scope
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.user_space.approval_mode import (
    ApprovalModeError,
    autonomous_tool_decision,
    execute_approval_mode_operation,
    permission_config,
    read_approval_mode,
    read_permission_mode,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_model_profiles import Host


def owner_home(root, name="alice", *, admin=False):
    identity = OwnerIdentity.local_main() if admin else OwnerIdentity.provider_user("tui", name)
    return home_paths_with_owner(home_paths(root), resolve_owner_home(root, identity))


def test_mode_roundtrip_preserves_policy_and_owner_isolation(tmp_path):
    alice, bob = owner_home(tmp_path), owner_home(tmp_path, "bob")
    assert read_permission_mode(alice) == "ask"
    assert not alice.owner_tool_policy_json.exists()
    execute_approval_mode_operation(alice, "set", "auto")
    data = json.loads(alice.owner_tool_policy_json.read_text())
    data["disabled_tools"] = ["example"]
    data["extra"] = {"preserve": True}
    alice.owner_tool_policy_json.write_text(json.dumps(data))
    execute_approval_mode_operation(alice, "set", "ask")
    after = json.loads(alice.owner_tool_policy_json.read_text())
    assert after["disabled_tools"] == ["example"] and after["extra"] == {"preserve": True}
    assert read_approval_mode(bob) == "ask" and not bob.owner_tool_policy_json.exists()
    assert alice.owner_tool_policy_json.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", ["unknown", [], {}, None, 1, True])
def test_invalid_mode_fails_closed_without_overwrite(tmp_path, bad):
    home = owner_home(tmp_path)
    execute_approval_mode_operation(home, "set", "ask")
    path = home.owner_tool_policy_json
    data = json.loads(path.read_text())
    data["permission_mode"] = bad
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ApprovalModeError):
        read_permission_mode(home)
    with pytest.raises(ApprovalModeError):
        execute_approval_mode_operation(home, "set", "auto")
    assert path.read_bytes() == before


def test_full_access_requires_real_admin_even_for_forged_file(tmp_path):
    ordinary = owner_home(tmp_path, "admin")
    with pytest.raises(ApprovalModeError):
        execute_approval_mode_operation(ordinary, "set", "full-access")
    execute_approval_mode_operation(ordinary, "set", "ask")
    data = json.loads(ordinary.owner_tool_policy_json.read_text())
    data["permission_mode"] = "full-access"
    ordinary.owner_tool_policy_json.write_text(json.dumps(data))
    with pytest.raises(ApprovalModeError):
        permission_config(AgentConfig(), ordinary)
    admin = owner_home(tmp_path, admin=True)
    result = execute_approval_mode_operation(admin, "set", "full-access")
    assert result["is_admin"] and read_approval_mode(admin) == "auto"


def test_permission_config_preserves_deployment_and_caps_children(tmp_path):
    home = owner_home(tmp_path, admin=True)
    config = AgentConfig(access_mode="full-access", path_access_mode="full")
    assert permission_config(config, home) is config
    child = permission_config(config, home, inherited=True)
    assert (child.access_mode, child.path_access_mode) == ("workspace-write", "normal")
    execute_approval_mode_operation(home, "set", "auto")
    assert permission_config(config, home).access_mode == "workspace-write"
    execute_approval_mode_operation(home, "set", "full-access")
    assert permission_config(config, home) is config
    restricted = permission_config(AgentConfig(access_mode="restricted"), home)
    assert restricted.access_mode == "restricted" and restricted.path_access_mode == "normal"


@pytest.mark.parametrize("owner_type", ["main_agent", "subagent"])
@pytest.mark.parametrize("approval,mode,expected", [
    ("dangerous", "ask", "ask"), ("dangerous", "auto", "allow"),
    ("always", "auto", "ask"), ("dangerous", "unavailable", "deny"),
])
def test_tool_policy_owner_mode_for_main_and_child(tmp_path, owner_type, approval, mode, expected):
    tool = SimpleNamespace(model_spec=make_test_model_spec("approval_probe"),
                           runtime_policy=make_test_runtime_policy("dangerous", approval_mode=approval))
    snapshot = runtime_snapshot_for_tools({"approval_probe": tool}, owner_type=owner_type)
    call = canonical_test_call(snapshot, "approval_probe", {})
    result = ActionPolicy().decide(ActionPolicyRequest(call, snapshot, tmp_path, approval_mode=mode))
    assert result.status == expected
    if expected == "allow":
        assert result.evidence["approval_mode"] == "auto"


def test_auto_still_rejects_unavailable_tools_and_invalid_arguments(tmp_path):
    tool = SimpleNamespace(model_spec=make_test_model_spec("approval_probe"),
                           runtime_policy=make_test_runtime_policy("dangerous"))
    snapshot = runtime_snapshot_for_tools({"approval_probe": tool})
    for name, args, code in [("missing", {}, "TOOL_NOT_IN_RUNTIME_SNAPSHOT"),
                             ("approval_probe", {"unexpected": True}, "TOOL_INVALID_ARGUMENTS")]:
        result = ActionPolicy().decide(ActionPolicyRequest(
            canonical_test_call(snapshot, name, args), snapshot, tmp_path, approval_mode="auto"))
        assert result.status == "deny" and code in result.reason_codes


def test_permission_only_scope_keeps_backend_and_restores_snapshot(tmp_path):
    host = Host(tmp_path / "config")
    host.home_paths = owner_home(tmp_path, admin=True)
    original_config, original_backend = host.config, host.backend
    execute_approval_mode_operation(host.home_paths, "set", "full-access")
    with selected_model_scope(host):
        assert host.config.access_mode == "full-access" and host.backend is original_backend
        execute_approval_mode_operation(host.home_paths, "set", "auto")
        assert host.config.access_mode == "full-access"
    assert host.config is original_config
    with selected_model_scope(host):
        assert host.config.access_mode == "workspace-write"
    host.config = AgentConfig(access_mode="full-access", path_access_mode="full")
    with selected_model_scope(host, inherited=True):
        assert host.config.access_mode == "workspace-write" and host.backend is original_backend


def test_pending_child_resumes_by_owner_mode_without_waiting_for_parent(tmp_path):
    from agent_py_agent.agent.conversation.agent_tool_approval import (
        publish_agent_tool_approval,
        wait_for_agent_tool_approval,
    )
    from agent_py_agent.tests.test_gateway_agent_control_service import (
        _bound_agent_tree,
        _child_approval_request,
    )

    agent, _scope, child = _bound_agent_tree(tmp_path)
    request = _child_approval_request(child.id)
    handle = publish_agent_tool_approval(agent, run_id=child.id,
                                           thread_id=child.agent_thread_id, request_value=request)
    assert autonomous_tool_decision(agent, request) is None
    execute_approval_mode_operation(agent.home_paths, "set", "auto")
    decision = wait_for_agent_tool_approval(
        handle, discovery_seconds=0, mode_decision_provider=partial(autonomous_tool_decision, agent))
    assert decision.permission_id == request.permission_id and decision.decision == "approved"
    assert not handle.path.exists()


def test_pending_main_mode_resume_preserves_deny_and_cancel_priority(tmp_path):
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.gateway_parts.permission_bridge import (
        wait_for_gateway_permission_decision,
        write_gateway_permission_decision,
    )
    from agent_py_agent.tests.test_gateway_agent_control_service import _child_approval_request

    request = _child_approval_request("main-run")
    path = tmp_path / "chunks.jsonl"
    def provider(req):
        return ToolApprovalDecision(req.permission_id, "approved")
    assert wait_for_gateway_permission_decision(path, request, mode_decision_provider=provider).decision == "approved"
    write_gateway_permission_decision(path, request, ToolApprovalDecision(request.permission_id, "denied"))
    assert wait_for_gateway_permission_decision(path, request, mode_decision_provider=provider).decision == "denied"
    result = wait_for_gateway_permission_decision(path, request, mode_decision_provider=provider,
                                                 cancellation_token=SimpleNamespace(cancelled=True))
    assert result.decision == "cancelled"


def test_permission_registry_view_keeps_original_and_owner_boundary(tmp_path):
    from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry

    root = tmp_path / "owner"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    base = _registry(root).with_access_policy(
        access_mode="workspace-write", path_access_mode="normal", owner_scope_root=str(root))
    full = base.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
    assert full is not base and base.owner_scope_root == str(root)
    assert full.tools["tool_search"].registry is full
    assert not _execute(base, "read_file", {"path": str(outside)}).ok
    assert _execute(full, "read_file", {"path": str(outside)}).ok


def test_parallel_tools_keep_selected_model_and_permission_snapshot(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import _execute_parallel_segment
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_tool_round_execution import _round_request, _success

    host = Host(tmp_path / "config")
    host.home_paths = owner_home(tmp_path, admin=True)
    profile_id, _ = add(host, model_name="deepseek-v4-flash")
    execute_model_profile_operation(host, "set_default", {"profile_id": profile_id})
    execute_approval_mode_operation(host.home_paths, "set", "full-access")
    observations = []

    def execute(request):
        observations.append((host.config.model_name, host.config.access_mode))
        return _success(request, "ok")

    request = _round_request(agent=host, params=SimpleNamespace(tool_context=[]), tool_rounds=1,
                            response=ModelResponse(text="", backend="test"),
                            calls=[{"tool": "read_file", "path": "a"}, {"tool": "read_file", "path": "b"}],
                            execute_one=execute, record_one=lambda record: None)
    with selected_model_scope(host):
        _execute_parallel_segment(request, list(request.calls), start_idx=0)
    assert observations == [("deepseek-v4-flash", "full-access")] * 2
    assert host.config.model_name == "deployment-model"
