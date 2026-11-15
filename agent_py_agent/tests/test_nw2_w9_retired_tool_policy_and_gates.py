"""NW2-W9 回归测试：已退役子代理控制工具 fail-closed 诊断与 GATE-01 契约对齐。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_templates import RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
    scheduled_child_create_params,
)
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
    stripped_retired_explicit_tools,
)
from agent_py_agent.agent.tooling.models import tool_schema_hash


def test_stripped_retired_explicit_tools_handles_mixed_and_invalid_inputs():
    assert stripped_retired_explicit_tools(None) == ()
    assert stripped_retired_explicit_tools(SimpleNamespace(allowed_tools=None)) == ()
    assert stripped_retired_explicit_tools(SimpleNamespace(allowed_tools=[])) == ()
    assert stripped_retired_explicit_tools(SimpleNamespace(allowed_tools=["  ", ""])) == ()
    assert stripped_retired_explicit_tools(SimpleNamespace(allowed_tools="schedule_child_subagents")) == ()

    mixed = HierarchyChildSpec(
        goal="测试混合工具清洗",
        allowed_tools=[
            " schedule_child_subagents ",
            "create_subagents",
            "dispatch_subagents",
            "schedule_child_subagents",
            "read_file",
            "inspect_agent_tree",
            "raise_event",
        ],
    )
    assert stripped_retired_explicit_tools(mixed) == (
        "schedule_child_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
        "raise_event",
    )

    from_set = SimpleNamespace(allowed_tools={"raise_event", "dispatch_subagents", "read_file"})
    assert stripped_retired_explicit_tools(from_set) == ("dispatch_subagents", "raise_event")
    assert set(stripped_retired_explicit_tools(SimpleNamespace(allowed_tools=list(RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS)))) == set(
        RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS
    )


def test_scheduled_child_tools_fail_closed_on_all_retired_explicit_tools():
    parent_tools = ["read_file", "write_file", "create_subagents", "list_agents", "cancel_subagents"]
    retired_only = list(RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS)

    for role in ("worker", "coordinator"):
        spec = HierarchyChildSpec(goal="实现交付物", role=role, allowed_tools=list(retired_only))
        tools = scheduled_child_tools(
            ToolPolicyRequest(
                parent_tools=parent_tools,
                spec=spec,
                extra_write_roots=["/tmp/ws"],
                goal=spec.goal,
                role=role,
            )
        )
        assert isinstance(tools, list)
        assert tools == []

    mixed_spec = HierarchyChildSpec(
        goal="混合参数保留有效工具",
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "create_subagents", "read_file"],
    )
    mixed_tools = scheduled_child_tools(
        ToolPolicyRequest(
            parent_tools=parent_tools,
            spec=mixed_spec,
            extra_write_roots=[],
            goal=mixed_spec.goal,
            role="coordinator",
        )
    )
    assert isinstance(mixed_tools, list)
    assert "create_subagents" in mixed_tools
    assert "read_file" in mixed_tools
    for retired in RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS:
        assert retired not in mixed_tools

    default_spec = HierarchyChildSpec(goal="默认继承列表", role="worker", allowed_tools=[])
    default_tools = scheduled_child_tools(
        ToolPolicyRequest(
            parent_tools=parent_tools,
            spec=default_spec,
            extra_write_roots=[],
            goal=default_spec.goal,
            role="worker",
        )
    )
    assert "read_file" in default_tools
    assert "write_file" in default_tools


def test_scheduler_records_retired_tool_diagnostics_without_mutating_spec_or_schema(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="协调调度任务",
        thought="root",
        plan=["plan"],
        role="coordinator",
        allowed_tools=["read_file", "write_file", "create_subagents", "list_agents", "cancel_subagents"],
    )

    caller_attrs = {"custom_note": "keep", "retired_explicit_tools_stripped": ["spoofed"]}
    retired_spec = HierarchyChildSpec(
        goal="使用已退役工具清单触发 fail-closed 诊断",
        role="coordinator",
        agent_name="lead-retired",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        attributes=caller_attrs,
    )
    clean_spec = HierarchyChildSpec(
        goal="正常未命中退役工具的子代理",
        role="coordinator",
        agent_name="lead-clean",
        allowed_tools=["create_subagents", "list_agents", "cancel_subagents"],
        attributes={"retired_explicit_tools_stripped": ["stale_value"]},
    )

    params_retired = scheduled_child_create_params(manager, root, retired_spec, sibling_index=0)
    assert params_retired.allowed_tools == []
    assert params_retired.attributes["retired_explicit_tools_stripped"] == [
        "schedule_child_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
    ]
    assert caller_attrs["retired_explicit_tools_stripped"] == ["spoofed"]

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[retired_spec, clean_spec],
            apply=True,
        )
    )
    assert result.blocked is False
    retired_child = manager.load(result.created_run_ids[0])
    clean_child = manager.load(result.created_run_ids[1])

    assert retired_child.allowed_tools == []
    assert retired_child.attributes["custom_note"] == "keep"
    assert retired_child.attributes["retired_explicit_tools_stripped"] == [
        "schedule_child_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
    ]
    guidance = retired_child.attributes["retired_explicit_tools_guidance"]
    for name in ("create_subagents", "list_agents", "inspect_agent_run", "cancel_agent"):
        assert name in str(guidance)

    assert "retired_explicit_tools_stripped" not in clean_child.attributes
    assert "retired_explicit_tools_guidance" not in clean_child.attributes
    assert "create_subagents" in clean_child.allowed_tools
    assert "list_agents" in clean_child.allowed_tools

    # 诊断字段不透传给孙代（仅对当级显式命中者记录）。
    grand_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=retired_child.id,
            child_specs=[HierarchyChildSpec(goal="孙代", role="worker", agent_name="leaf")],
            apply=True,
        )
    )
    grandchild = manager.load(grand_result.created_run_ids[0])
    assert "retired_explicit_tools_stripped" not in grandchild.attributes
    assert "retired_explicit_tools_guidance" not in grandchild.attributes

    # create_subagents schema hash 保持不变，未新增顶层 schema 字段。
    for media_enabled in (False, True):
        model_spec = build_create_subagents_model_spec(input_media=media_enabled)
        assert model_spec.schema_hash == tool_schema_hash(model_spec.input_schema)
        assert set(model_spec.input_schema["properties"].keys()) == {"items", "batch_summary"}
