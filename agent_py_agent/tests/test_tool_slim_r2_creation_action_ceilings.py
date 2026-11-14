"""R2-R1 回归测试：创建子代理、持久化重载、执行上下文组装与最终执行器全链路保留精确 tool:action 动作上界。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration import lifecycle
from agent_py_agent.agent.agent_core.orchestration.create_policy import (
    _within_parent_tool_authority,
    create_run_params,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import register_business_tools
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.role_templates import DIRECT_CHILD_CONTROL_TOOLS
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.tooling.registry_auth import (
    CATALOG_ONLY_TOOL_GRANT,
    action_ceilings,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


def _make_business_subagent_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    home_dir: Path | None = None,
    workspace_dir: Path | None = None,
    disabled_tools: list[str] | None = None,
) -> SimpleAgent:
    home = home_dir or (tmp_path / "home")
    workspace = workspace_dir or (tmp_path / "workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home),
        memory_path="memory.jsonl",
        execution_mode="local_unmanaged",
        business_spaces_enabled=True,
        enable_subagents=True,
    )
    agent = SimpleAgent(config, workspace)
    if disabled_tools:
        agent.tools.disabled_tool_names.update(disabled_tools)
    agent.config.business_host_ports_enabled = True
    agent.business_operation_store = agent.tools.operation_store
    agent._local_business_services_cache = None
    register_business_tools(agent)
    return agent


def _install_business_handler_counter(agent: SimpleAgent) -> list[str]:
    tool = agent.tools.tools["business_space"]
    observed_actions: list[str] = []
    original_execute = tool.execute
    original_execute_scoped = tool.execute_scoped

    def _counting_execute(params: dict[str, object]):
        observed_actions.append(str(params.get("action") or ""))
        return original_execute(params)

    def _counting_execute_scoped(params: dict[str, object], invocation):
        observed_actions.append(str(params.get("action") or ""))
        return original_execute_scoped(params, invocation)

    tool.execute = _counting_execute  # type: ignore[method-assign]
    tool.execute_scoped = _counting_execute_scoped  # type: ignore[method-assign]
    return observed_actions


@pytest.mark.parametrize(
    "requested_allowed_tools",
    [
        ["business_space", "business_space:spaces.list"],
        ["business_spaces_list"],
    ],
    ids=["canonical_action_pair", "retired_leaf_alias"],
)
def test_child_creation_preserves_single_action_ceiling_through_reload_and_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_allowed_tools: list[str],
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params, **_kw: {"status": "not_needed", "run_ids": []},
    )
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    parent_agent = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        home_dir=home,
        workspace_dir=workspace,
    )
    parent_snapshot = parent_agent.tools.runtime_snapshot(run_id="root-parent")
    parent_business_enum = parent_snapshot.runtime("business_space").model_spec.input_schema[
        "properties"
    ]["action"]["enum"]
    assert "spaces.list" in parent_business_enum
    assert "spaces.create" in parent_business_enum

    create_res = execute_registry_test_call(
        parent_agent.tools,
        "create_subagents",
        {
            "items": [
                {
                    "goal": "只读列出当前业务空间清单",
                    "role": "worker",
                    "allowed_tools": list(requested_allowed_tools),
                }
            ]
        },
        run_id="root-parent",
        call_id="call-create-child-ceiling",
        operation_id="op-create-child-ceiling",
        idempotency_key="idem-create-child-ceiling",
    )
    assert create_res.ok is True, create_res.output
    (child_id,) = json.loads(create_res.output)["created_run_ids"]

    # 使用全新 SimpleAgent 从持久化存储重载并组装执行上下文。
    reloaded_agent = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        home_dir=home,
        workspace_dir=workspace,
    )
    observed_actions = _install_business_handler_counter(reloaded_agent)

    stored_task = reloaded_agent.subagents.load(child_id)
    assert action_ceilings(stored_task.allowed_tools).get("business_space") == frozenset(
        {"spaces.list"}
    )
    assert not set(DIRECT_CHILD_CONTROL_TOOLS).intersection(stored_task.allowed_tools)

    exec_context = reloaded_agent.subagents.runner_context.build_execution_context(child_id)
    assert action_ceilings(exec_context.allowed_tools).get("business_space") == frozenset(
        {"spaces.list"}
    )

    child_snapshot = reloaded_agent.tools.runtime_snapshot(
        allowed_tools=exec_context.allowed_tools,
        run_id=child_id,
    )
    child_business = child_snapshot.runtime("business_space")
    assert child_business is not None
    assert child_business.model_spec.input_schema["properties"]["action"]["enum"] == [
        "spaces.list"
    ]

    # 已授权的只读动作 spaces.list 正常可用。
    list_res = execute_registry_test_call(
        reloaded_agent.tools,
        "business_space",
        {
            "action": "spaces.list",
            "request_id": "req-child-list-01",
            "params": {},
        },
        allowed_tools=exec_context.allowed_tools,
        write_boundary=exec_context.write_boundary,
        run_id=child_id,
        call_id="call-child-list-01",
    )
    assert list_res.ok is True, list_res.output
    assert observed_actions == ["spaces.list"]

    # 未授权的写动作 spaces.create（规范名与退役叶工具名）均在进入业务 handler 前被拒绝。
    create_canonical_res = execute_registry_test_call(
        reloaded_agent.tools,
        "business_space",
        {
            "action": "spaces.create",
            "request_id": "req-child-create-denied-01",
            "idempotency_key": "idem-child-create-denied-01",
            "params": {"name": "Unpermitted Space"},
        },
        allowed_tools=exec_context.allowed_tools,
        write_boundary=exec_context.write_boundary,
        run_id=child_id,
        call_id="call-child-create-denied-01",
        operation_id="op-child-create-denied-01",
        idempotency_key="idem-child-create-denied-01",
    )
    assert create_canonical_res.ok is False
    assert create_canonical_res.handler_executed is False
    assert create_canonical_res.effect_outcome == "not_started"
    assert observed_actions == ["spaces.list"]

    create_legacy_res = execute_registry_test_call(
        reloaded_agent.tools,
        "business_spaces_create",
        {
            "request_id": "req-child-create-denied-02",
            "idempotency_key": "idem-child-create-denied-02",
            "params": {"name": "Unpermitted Legacy Space"},
        },
        allowed_tools=exec_context.allowed_tools,
        write_boundary=exec_context.write_boundary,
        run_id=child_id,
        call_id="call-child-create-denied-02",
        operation_id="op-child-create-denied-02",
        idempotency_key="idem-child-create-denied-02",
    )
    assert create_legacy_res.ok is False
    assert create_legacy_res.handler_executed is False
    assert create_legacy_res.effect_outcome == "not_started"
    assert observed_actions == ["spaces.list"]


def test_narrowed_parent_ceiling_cannot_be_widened_by_child_or_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "auto_start_tasks",
        lambda _agent, _tasks, _params, **_kw: {"status": "not_needed", "run_ids": []},
    )
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    agent = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        home_dir=home,
        workspace_dir=workspace,
    )

    narrow_parent_snapshot = agent.tools.runtime_snapshot(
        allowed_tools=[
            "create_subagents",
            "read_file",
            "list_tools",
            "business_space",
            "business_space:spaces.list",
        ],
        run_id="narrow-parent",
    )
    created = CreateSubagentsTool(agent).execute_scoped(
        {
            "items": [
                {
                    "goal": "协调核查空间目录但不创建新空间",
                    "role": "coordinator",
                    "allowed_tools": [
                        "create_subagents",
                        "read_file",
                        "tool_search",
                        "business_space",
                        "business_space:spaces.list",
                        "business_space:spaces.create",
                    ],
                }
            ]
        },
        ToolInvocationContext(narrow_parent_snapshot),
    )
    assert created.ok is True, created.output
    (coord_id,) = json.loads(created.output)["created_run_ids"]

    reloaded = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        home_dir=home,
        workspace_dir=workspace,
    )
    coord_task = reloaded.subagents.load(coord_id)
    coord_context = reloaded.subagents.runner_context.build_execution_context(coord_id)
    assert action_ceilings(coord_context.allowed_tools).get("business_space") == frozenset(
        {"spaces.list"}
    )
    assert action_ceilings(coord_context.allowed_tools).get("tool_search") == frozenset(
        {"catalog"}
    )
    assert CATALOG_ONLY_TOOL_GRANT in coord_context.allowed_tools
    assert {"capability_request", "record_lesson"} <= set(coord_task.allowed_tools)
    assert "record_lesson" in coord_context.allowed_tools

    # 进一步从该已收窄协调者派发孙代理，请求裸 business_space 也不能放大上界。
    coord_snapshot = reloaded.tools.runtime_snapshot(
        allowed_tools=coord_context.allowed_tools,
        run_id=coord_id,
    )
    previous = set_current_subagent_context(
        reloaded,
        run_id=coord_id,
        task_attributes=coord_task.attributes,
    )
    try:
        grand_created = CreateSubagentsTool(reloaded).execute_scoped(
            {
                "items": [
                    {
                        "goal": "执行空间只读巡检",
                        "role": "worker",
                        "allowed_tools": ["read_file", "tool_search", "business_space"],
                    }
                ]
            },
            ToolInvocationContext(coord_snapshot),
        )
    finally:
        restore_current_subagent_context(reloaded, previous)
    assert grand_created.ok is True, grand_created.output
    (grand_id,) = json.loads(grand_created.output)["created_run_ids"]

    final_agent = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        home_dir=home,
        workspace_dir=workspace,
    )
    observed = _install_business_handler_counter(final_agent)
    grand_context = final_agent.subagents.runner_context.build_execution_context(grand_id)
    assert action_ceilings(grand_context.allowed_tools).get("business_space") == frozenset(
        {"spaces.list"}
    )
    assert action_ceilings(grand_context.allowed_tools).get("tool_search") == frozenset(
        {"catalog"}
    )

    ok_read = execute_registry_test_call(
        final_agent.tools,
        "business_space",
        {"action": "spaces.list", "request_id": "req-grand-list", "params": {}},
        allowed_tools=grand_context.allowed_tools,
        write_boundary=grand_context.write_boundary,
        run_id=grand_id,
        call_id="call-grand-list",
    )
    assert ok_read.ok is True, ok_read.output

    denied_write = execute_registry_test_call(
        final_agent.tools,
        "business_space",
        {
            "action": "spaces.create",
            "request_id": "req-grand-create",
            "idempotency_key": "idem-grand-create",
            "params": {"name": "Blocked Grandchild Space"},
        },
        allowed_tools=grand_context.allowed_tools,
        write_boundary=grand_context.write_boundary,
        run_id=grand_id,
        call_id="call-grand-create",
        operation_id="op-grand-create",
        idempotency_key="idem-grand-create",
    )
    assert denied_write.ok is False
    assert denied_write.handler_executed is False
    assert denied_write.effect_outcome == "not_started"
    assert observed == ["spaces.list"]


def test_preserves_empty_grants_owner_disabled_actions_catalog_and_child_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_business_subagent_host(
        tmp_path,
        monkeypatch,
        disabled_tools=["business_spaces_create"],
    )

    # 1) 显式空授权列表原样保持为空（窄角色与直接 _within_parent_tool_authority 调用均不扩权）。
    assert _within_parent_tool_authority(agent, []) == []
    empty_narrow = create_run_params(
        agent,
        {"role": "reviewer", "allowed_tools": [], "_exact_allowed_tools": True},
        "仅依赖输入上下文审阅",
        [],
    )
    assert empty_narrow.allowed_tools == []

    # 2) owner 禁用的退役叶动作不能通过请求该动作或包含该动作的组合被放行。
    only_disabled_action = _within_parent_tool_authority(
        agent,
        ["business_space", "business_space:spaces.create"],
    )
    assert "business_space" not in only_disabled_action
    mixed_with_disabled = _within_parent_tool_authority(
        agent,
        ["business_space", "business_space:spaces.list", "business_space:spaces.create"],
    )
    assert action_ceilings(mixed_with_disabled).get("business_space") == frozenset(
        {"spaces.list"}
    )

    # 3) list_tools 归一为 tool_search + tool_search:catalog，同时保留就绪的 child-only 控制工具。
    catalog_and_controls = _within_parent_tool_authority(
        agent,
        ["list_tools", "read_file", "capability_request", "record_lesson"],
    )
    assert "list_tools" not in catalog_and_controls
    assert {"tool_search", CATALOG_ONLY_TOOL_GRANT, "read_file", "capability_request", "record_lesson"} <= set(
        catalog_and_controls
    )

    # 4) 旧版缺少 tool_grants 的绑定快照通过 creation_snapshot_tool_grants 保持目录只读上界。
    with pytest.MonkeyPatch.context() as mp:
        for mod_path in (
            "agent_py_agent.agent.agent_core.orchestration.create_policy.current_creation_tool_authority",
            "agent_py_agent.agent.subagents.capability_scope.current_creation_tool_authority",
        ):
            mp.setattr(
                mod_path,
                lambda: {
                    "schema_version": "direct_parent_tool_authority.v1",
                    "available_tool_names": ["read_file", "tool_search"],
                },
            )
        v1_authority_grants = _within_parent_tool_authority(agent, ["read_file", "tool_search"])
    assert {"read_file", "tool_search", CATALOG_ONLY_TOOL_GRANT} <= set(v1_authority_grants)

    # 5) 父级子代理显式禁用的 child-only 控制入口不能在下一层复活。
    parent = agent.subagents.create_run(
        goal="父级任务",
        allowed_tools=["read_file", "capability_request"],
    )
    parent.effective_permissions["disabled_tools"] = ["capability_request"]
    agent.subagents.save(parent)
    previous = set_current_subagent_context(agent, run_id=parent.id)
    try:
        filtered = _within_parent_tool_authority(
            agent,
            ["read_file", "capability_request"],
        )
        assert filtered == ["read_file"]
    finally:
        restore_current_subagent_context(agent, previous)

    # 6) owner 禁用整个域工具名时，任何子动作组合均不放行。
    agent.tools.disabled_tool_names.add("business_space")
    assert _within_parent_tool_authority(agent, ["business_spaces_list"]) == []
