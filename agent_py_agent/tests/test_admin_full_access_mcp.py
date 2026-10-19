"""管理员当前全盘模式仅免普通 MCP 确认；使用真实注册表和代理，供应商完全离线。"""
from dataclasses import replace
from functools import partial

import pytest

from agent_py_agent.agent.plugin_runtime import PluginProxyTool
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
from agent_py_agent.agent.tooling.models import ApprovalPolicy
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.approval_mode import (
    execute_approval_mode_operation,
    read_permission_state,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call


# LLM: 仅模拟供应商传输；不跳过正式 MCP handler、注册表、权限门或审批链。
# 类用途: 离线记录真实代理是否实际调用外部工具。
class FakeMCPClient:
    # LLM: 测试数据只存在用例临时目录；不启动服务或读取凭据。
    # 函数用途: 初始化调用记录。
    def __init__(self):
        self.calls = []

    # LLM: 只提供连接状态事实；不建立真实连接。
    # 函数用途: 让原 MCP 可用性门检查一个健康假连接。
    def is_running(self):
        return True

    # LLM: 原 handler 真实到达此入口才算执行；不做网络副作用。
    # 函数用途: 记录供应商请求并返回合法成功结果。
    def call_tool(self, name, arguments, **options):
        self.calls.append((name, arguments))
        return {"content": "offline provider executed", "isError": False}


# LLM: 用真实 owner 策略与 registry 装配生产 reader；权限模式只沿正式写入口保存到测试 home。
# 函数用途: 创建离线工具调用场景，可逐项改变有效墙、身份或插件实现来验证权限边界。
def mcp_case(tmp_path, *, identity=None, mode="full-access", scope="", access="full-access", path="full", actor="main_agent", plugin=False, reader=None):
    identity = identity or OwnerIdentity.local_main()
    home = home_paths_with_owner(home_paths(tmp_path / "home"), resolve_owner_home(tmp_path / "home", identity))
    if identity.owner_kind != "group":
        execute_approval_mode_operation(home, "set", mode)
    else:
        assert read_permission_state(home)["mode"] == mode
    registry = ToolRegistry(ToolRegistryParams(workspace_root=tmp_path, max_chars=6000, max_entries=100,
        max_matches=30, web_max_chars=12000, http_timeout=30, catalog_limit=20, retrieval_limit=3,
        vector_search_enabled=False, owner_scope_root=scope, access_mode=access, path_access_mode=path,
        owner_type=actor, operation_store_required=False,
        approval_mode_reader=reader,
        permission_state_reader=None if reader else partial(read_permission_state, home)))
    client = FakeMCPClient()
    tool = build_proxy_tool(client, "offline", MCPToolInfo("act", "offline action", {"type": "object", "properties": {}, "additionalProperties": False}))
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    if plugin:
        tool = PluginProxyTool(client, tool.remote_tool, tool.model_spec, tool.runtime_policy)
    registry.register(tool)
    return home, registry, client, tool


# LLM: 每次从真实注册表生成同一政策快照，调用原 execute_tool；不手工构造 admin bool。
# 函数用途: 发出一次独立操作，验证实际 handler 是否到达供应商。
def invoke(registry, tool, *, boundary=None, context=None):
    snapshot = registry.runtime_snapshot(run_id="main-run")
    call = canonical_test_call(snapshot, tool.model_spec.name, {})
    return registry.execute_tool(call, write_boundary=boundary, runtime_snapshot=snapshot, trusted_run_context=context)


def test_current_admin_full_access_executes_always_mcp_without_confirmation(tmp_path):
    home, registry, client, tool = mcp_case(tmp_path)
    assert invoke(registry, tool).result.ok
    assert len(client.calls) == 1
    execute_approval_mode_operation(home, "set", "auto")
    assert invoke(registry, tool).decision.status == "ask"
    assert len(client.calls) == 1


@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_admin_without_explicit_full_access_still_asks(tmp_path, mode):
    _, registry, client, tool = mcp_case(tmp_path, mode=mode)
    assert invoke(registry, tool).decision.status == "ask" and not client.calls


@pytest.mark.parametrize("identity", [OwnerIdentity.provider_user("feishu", "admin"), OwnerIdentity.provider_group("feishu", "room")])
def test_ordinary_user_and_group_auto_do_not_borrow_admin_privilege(tmp_path, identity):
    _, registry, client, tool = mcp_case(tmp_path, identity=identity, mode="auto")
    assert invoke(registry, tool).decision.status == "ask" and not client.calls


@pytest.mark.parametrize("options", [{"scope": "owner-wall"}, {"access": "restricted"}, {"path": "normal"}, {"actor": "subagent"}, {"plugin": True}, {"reader": lambda: "auto"}])
def test_effective_scope_and_ordinary_handler_are_required(tmp_path, options):
    _, registry, client, tool = mcp_case(tmp_path, **options)
    assert invoke(registry, tool).decision.status == "ask" and not client.calls


def test_model_and_restored_boundary_cannot_set_admin_fact(tmp_path):
    _, registry, client, tool = mcp_case(tmp_path, mode="auto")
    forged = {"admin_full_access": True, "permission_mode": "full-access", "is_admin": True}
    assert invoke(registry, tool, boundary=forged, context=forged).decision.status == "ask"
    assert not client.calls


def test_disabled_tool_and_schema_denial_survive_admin_full_access(tmp_path):
    _, registry, client, tool = mcp_case(tmp_path)
    snapshot = registry.runtime_snapshot(run_id="main-run")
    call = canonical_test_call(snapshot, tool.model_spec.name, {"admin_full_access": True})
    denied = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot)
    assert denied.decision.status == "deny" and not client.calls
    registry.disabled_tool_names.add(tool.model_spec.name)
    denied = registry.execute_tool(canonical_test_call(snapshot, tool.model_spec.name, {}), write_boundary=None,
        runtime_snapshot=registry.runtime_snapshot(run_id="main-run"))
    assert denied.decision.status == "deny" and not client.calls


def test_current_child_boundary_cannot_borrow_parent_full_access(tmp_path):
    _, registry, client, tool = mcp_case(tmp_path)
    boundary = {"effective_owner_scope_root": str(tmp_path / "child-home")}
    assert invoke(registry, tool, boundary=boundary).decision.status == "ask"
    assert not client.calls


def test_non_mcp_always_tool_retains_confirmation(tmp_path):
    _, registry, client, _ = mcp_case(tmp_path)
    tool = registry.tools["read_file"]
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    snapshot = registry.runtime_snapshot(run_id="main-run")
    call = canonical_test_call(snapshot, tool.model_spec.name, {"path": str(tmp_path / "business.txt")})
    result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot)
    assert result.decision.status == "ask" and not client.calls


def test_corrupt_owner_policy_fails_closed_at_real_reader(tmp_path):
    home, registry, client, tool = mcp_case(tmp_path)
    home.owner_tool_policy_json.write_text("not JSON")
    result = invoke(registry, tool)
    assert result.decision.status == "deny" and not client.calls


def test_production_agent_reader_preserves_current_admin_full_fact(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "real-home"),
        access_mode="full-access", path_access_mode="full", enable_plugins=False), tmp_path)
    execute_approval_mode_operation(agent.home_paths, "set", "full-access")
    client = FakeMCPClient()
    tool = build_proxy_tool(client, "offline", MCPToolInfo("act", "offline action", {"type": "object", "properties": {}}))
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    agent.tools.register(tool)
    repo = agent.subagents.runtime_db
    chain = repo.record_run_creation(owner_id=agent.tools.operation_owner_id, goal="合成 MCP 权限回归",
        conversation_task_id="mcp-task", thread_id="mcp-thread", run_id="mcp-managed-run", role="assistant")
    snapshot = agent.tools.runtime_snapshot(run_id="mcp-managed-run")
    call = canonical_test_call(snapshot, tool.model_spec.name, {}, attempt_id=chain["attempt_id"])
    result = agent.tools.execute_tool(call, write_boundary={"task_id": "mcp-task"}, runtime_snapshot=snapshot)
    assert result.decision.status == "allow", result.decision.to_dict()
    assert result.result.ok and len(client.calls) == 1, result.result
    with repo._runtime_connection() as connection:
        row = connection.execute("SELECT status FROM tool_operations WHERE operation_id=?", (call.operation_id,)).fetchone()
    assert row["status"] == "SUCCEEDED"
    repo.create_attempt(chain["agent_run_id"])
    stale = replace(call, call_id="old-attempt-new-call", operation_id="old-attempt-new-operation", idempotency_key="old-attempt-new-key")
    rejected = agent.tools.execute_tool(stale, write_boundary={"task_id": "mcp-task"}, runtime_snapshot=snapshot)
    assert not rejected.result.ok and len(client.calls) == 1


@pytest.mark.parametrize("state", [None, "auto", {}, {"mode": "full-access", "is_admin": "true", "approval_mode": "auto"},
    {"mode": "full-access", "is_admin": False, "approval_mode": "auto"},
    {"mode": "ask", "is_admin": True, "approval_mode": "auto"},
    {"mode": "full-access", "is_admin": True, "approval_mode": []}])
def test_invalid_new_reader_never_falls_back_to_old_auto(tmp_path, state):
    _, registry, client, tool = mcp_case(tmp_path)
    registry._construction_params = replace(registry._construction_params, permission_state_reader=lambda: state)
    registry.approval_mode_reader = lambda: pytest.fail("坏新读口不能转旧读口放行")
    assert invoke(registry, tool).decision.status == "deny" and not client.calls


def test_permission_state_is_read_once_for_one_call(tmp_path):
    home, registry, client, tool = mcp_case(tmp_path)
    calls = []
    state = read_permission_state(home)
    def read_once():
        calls.append(None)
        assert len(calls) == 1
        return state
    registry._construction_params = replace(registry._construction_params, permission_state_reader=read_once)
    registry.approval_mode_reader = lambda: pytest.fail("同调用不得读两份模式")
    assert invoke(registry, tool).result.ok
    assert len(client.calls) == 1


def test_subagent_snapshot_without_boundary_does_not_borrow_full_access(tmp_path):
    _, registry, client, tool = mcp_case(tmp_path)
    snapshot = replace(registry.runtime_snapshot(run_id="child-run"), owner_type="subagent", snapshot_hash="")
    call = canonical_test_call(snapshot, tool.model_spec.name, {})
    result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot)
    assert result.decision.status == "ask" and not client.calls
