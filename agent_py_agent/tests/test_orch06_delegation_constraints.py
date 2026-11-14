"""Unit and integration tests for enforceable structured delegation constraints (ORCH-06).

Tests cover:
1. Cumulative created-child limit != concurrent active count (terminated/cancelled children still count);
2. Descendant policy & max depth rejection (SUBAGENT_DESCENDANTS_FORBIDDEN);
3. DELEGATION_SCOPE_EXCEEDS_PARENT & TOOL_PERMISSION_DENIED upper-bound checks;
4. exact_write_set allowing listed file while blocking unlisted sibling file in same folder,
   plus allowed_output_dirs, allowed_read_inputs, permitted_tools, prohibited_actions,
   and honest external_instruction_only vs unknown_enforcement_fields reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.hierarchy_tools import execute_child_creation
from agent_py_agent.agent.agent_core.orchestration.capacity import (
    available_creation_slots,
    checked_creation_capacity,
    cumulative_created_child_count,
    subagent_quota_result,
)
from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
    resolved_extra_write_roots,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import (
    create_run_params,
    create_task_attributes,
)
from agent_py_agent.agent.agent_core.orchestration.create_read_scope import (
    ChildReadScope,
)
from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
    DelegationConstraintError,
    DelegationEnforcementReport,
    DelegationScopeExceedsParentError,
    StructuredDelegationConstraints,
    ToolPermissionDeniedError,
    evaluate_delegation_enforcement,
    extract_delegation_constraints,
    validate_delegation_constraints_upper_bound,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.runtime_context import set_current_subagent_context
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import HierarchyChildSpec
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)
from agent_py_agent.agent.subagents.services.hierarchy.write_policy import (
    ChildWriteRootRequest,
    requested_child_write_roots,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

# -----------------------------------------------------------------------------
# 1. Dataclasses, serialization, and enforcement reporting
# -----------------------------------------------------------------------------


def test_structured_delegation_constraints_defaults_and_roundtrip():
    """Verify defaults, dict serialization, and from_dict roundtrip."""
    default_c = StructuredDelegationConstraints()
    assert default_c.schema_version == "delegation_constraints.v1"
    assert default_c.actor_runtime == "native"
    assert default_c.max_cumulative_children is None
    assert default_c.allow_descendants is True
    assert default_c.max_descendant_depth is None
    assert default_c.exact_write_set == ()
    assert default_c.allowed_read_inputs == ()
    assert default_c.allowed_output_dirs == ()
    assert default_c.permitted_tools == ()
    assert default_c.prohibited_actions == ()

    custom = StructuredDelegationConstraints(
        actor_runtime="native",
        max_cumulative_children=3,
        allow_descendants=False,
        max_descendant_depth=2,
        exact_write_set=("/tmp/ws/file1.py", "/tmp/ws/file2.py"),
        allowed_read_inputs=("/tmp/ws/input.txt",),
        allowed_output_dirs=("/tmp/ws/output",),
        permitted_tools=("read_file", "write_file"),
        prohibited_actions=("git_push", "spawn_children"),
    )
    as_dict = custom.to_dict()
    restored = StructuredDelegationConstraints.from_dict(as_dict)
    assert restored == custom


def test_delegation_enforcement_report_external_instruction_only():
    """When actor_runtime is external_instruction_only, report honestly classifies all constraints as instruction_only."""
    constraints = StructuredDelegationConstraints(
        actor_runtime="external_instruction_only",
        max_cumulative_children=1,
        allow_descendants=False,
        exact_write_set=("/tmp/ws/file.py",),
        permitted_tools=("read_file",),
        prohibited_actions=("git_push", "custom_action"),
    )
    report = evaluate_delegation_enforcement(constraints)
    assert report.enforced_fields == ()
    assert report.unknown_enforcement_fields == ()
    assert "max_cumulative_children" in report.instruction_only_fields
    assert "allow_descendants" in report.instruction_only_fields
    assert "exact_write_set" in report.instruction_only_fields
    assert "permitted_tools" in report.instruction_only_fields
    assert "prohibited_actions" in report.instruction_only_fields


def test_delegation_enforcement_report_native_recognized_and_unknown():
    """Native actor reports enforced fields, and unrecognized action tokens appear in unknown_enforcement_fields."""
    constraints = StructuredDelegationConstraints(
        actor_runtime="native",
        max_cumulative_children=2,
        allow_descendants=False,
        exact_write_set=("/tmp/ws/file.py",),
        permitted_tools=("read_file",),
        prohibited_actions=("git_push", "quantum_teleport", "arbitrary_custom_op"),
    )
    report = evaluate_delegation_enforcement(constraints)
    assert "max_cumulative_children" in report.enforced_fields
    assert "allow_descendants" in report.enforced_fields
    assert "exact_write_set" in report.enforced_fields
    assert "permitted_tools" in report.enforced_fields
    assert "prohibited_actions" in report.enforced_fields
    assert report.instruction_only_fields == ()
    assert "quantum_teleport" in report.unknown_enforcement_fields
    assert "arbitrary_custom_op" in report.unknown_enforcement_fields


# -----------------------------------------------------------------------------
# 2. Cumulative created-child limit != concurrent active count
# -----------------------------------------------------------------------------


def test_cumulative_child_limit_rejects_after_child_terminated(tmp_path):
    """Cumulative limit=1: creating 1 child, then terminating/cancelling it still rejects a 2nd child."""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(
        goal="parent",
        thought="plan",
        plan=["plan"],
    )
    # Set parent constraint max_cumulative_children=1
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=1,
    ).to_dict()
    manager.save(parent)

    # Initialize runtime.db and record parent run
    db_path = tmp_path / "runtime.db"
    repo = RuntimeRepository(db_path)
    manager.runtime_db = repo

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=repo,
        config=SimpleNamespace(enable_subagents=True, max_subagents=10),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    # Before creating any child: cumulative count is 0, remaining slots = 1
    assert cumulative_created_child_count(agent, parent.id) == 0
    slots, limits = available_creation_slots(agent)
    assert slots == 1
    assert limits["cumulative_cap"] == 1
    assert limits["remaining_cumulative"] == 1

    # Create 1 child
    child1 = manager.create_run(
        goal="child 1",
        thought="child 1",
        plan=["c1"],
        parent_id=parent.id,
    )
    parent.child_ids.append(child1.id)
    manager.save(parent)

    # Record delegation in runtime.db delegations table
    with repo._runtime_connection() as conn:
        conn.execute(
            """INSERT INTO delegations (
                delegation_id, parent_agent_run_id, child_agent_run_id,
                child_attempt_id, granted_scope_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "del-1",
                parent.id,
                child1.id,
                "att-1",
                json.dumps(parent.attributes["delegation_constraints"]),
                1000.0,
            ),
        )

    # Now cumulative count is 1
    assert cumulative_created_child_count(agent, parent.id) == 1

    # Even while child 1 is active, slots should be 0
    slots, limits = available_creation_slots(agent)
    assert slots == 0
    assert limits["remaining_cumulative"] == 0

    # TERMINATE / CANCEL child 1 (active count goes to 0)
    child1.status = "CANCELLED"
    manager.save(child1)

    # Crucial assertion: terminated/cancelled child STILL counts toward cumulative limit!
    assert cumulative_created_child_count(agent, parent.id) == 1
    slots, limits = available_creation_slots(agent)
    assert slots == 0
    assert limits["remaining_cumulative"] == 0

    # Attempting to create a 2nd child must be rejected with SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED
    outcome = subagent_quota_result(requested=1, available=0, limits=limits)
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"
    assert outcome.effect_outcome == "not_started"
    parsed_payload = json.loads(outcome.output)
    assert parsed_payload["error_code"] == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"


def test_cumulative_limit_zero_rejects_atomically(tmp_path):
    """When max_cumulative_children=0, creating even 1 child fails closed immediately."""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="p", thought="t", plan=["p"])
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=0,
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=None,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    slots, limits = available_creation_slots(agent)
    assert slots == 0
    assert limits["cumulative_cap"] == 0
    outcome = subagent_quota_result(requested=1, available=0, limits=limits)
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"
    assert outcome.effect_outcome == "not_started"
    assert json.loads(outcome.output)["error_code"] == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"


# -----------------------------------------------------------------------------
# 3. Descendant policy & depth enforcement (SUBAGENT_DESCENDANTS_FORBIDDEN)
# -----------------------------------------------------------------------------


def test_hierarchy_execute_child_creation_rejects_when_allow_descendants_false(tmp_path):
    """When parent has allow_descendants=False, execute_child_creation rejects with SUBAGENT_DESCENDANTS_FORBIDDEN before preparation."""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="p", thought="t", plan=["p"])
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        allow_descendants=False,
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=None,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    outcome = execute_child_creation(
        agent,
        {"children": [{"goal": "grandchild", "role": "worker"}]},
    )
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_DESCENDANTS_FORBIDDEN"
    assert outcome.effect_outcome == "not_started"
    assert json.loads(outcome.output)["error_code"] == "SUBAGENT_DESCENDANTS_FORBIDDEN"
    # Ensure no child was materialized
    assert len(manager.load(parent.id).child_ids) == 0


def test_hierarchy_execute_child_creation_rejects_when_remaining_depth_exhausted(tmp_path):
    """剩余代数为0时拒绝；绝对树深度不能替代已经逐代递减的余量。"""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="p", thought="t", plan=["p"])
    parent.depth = 2
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        allow_descendants=True,
        max_descendant_depth=0,  # 当前剩余为0，与parent.depth=2无关
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=None,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    outcome = execute_child_creation(
        agent,
        {"children": [{"goal": "grandchild", "role": "worker"}]},
    )
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_DESCENDANTS_FORBIDDEN"
    assert outcome.effect_outcome == "not_started"
    assert json.loads(outcome.output)["error_code"] == "SUBAGENT_DESCENDANTS_FORBIDDEN"


def test_hierarchy_execute_child_creation_rejects_when_spawn_children_prohibited(tmp_path):
    """When parent has 'spawn_children' in prohibited_actions, execute_child_creation rejects with SUBAGENT_DESCENDANTS_FORBIDDEN."""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="p", thought="t", plan=["p"])
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        prohibited_actions=("spawn_children",),
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=None,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    outcome = execute_child_creation(
        agent,
        {"children": [{"goal": "grandchild", "role": "worker"}]},
    )
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_DESCENDANTS_FORBIDDEN"
    assert outcome.effect_outcome == "not_started"
    assert json.loads(outcome.output)["error_code"] == "SUBAGENT_DESCENDANTS_FORBIDDEN"


# -----------------------------------------------------------------------------
# 4. Upper-bound preservation (monotone narrowing only)
# -----------------------------------------------------------------------------


def test_upper_bound_tool_outside_parent_authority_fails():
    """Child permitted_tools with tool outside parent authority fails with TOOL_PERMISSION_DENIED."""
    constraints = StructuredDelegationConstraints(
        permitted_tools=("read_file", "git_push_danger_tool"),
    )
    valid, err_msg = validate_delegation_constraints_upper_bound(
        constraints,
        parent_tools=["read_file", "search_text", "write_file"],
    )
    assert valid is False
    assert "TOOL_PERMISSION_DENIED" in err_msg
    assert "git_push_danger_tool" in err_msg


def test_upper_bound_write_path_outside_parent_roots_fails():
    """Child exact_write_set with path outside parent write roots fails with DELEGATION_SCOPE_EXCEEDS_PARENT."""
    constraints = StructuredDelegationConstraints(
        exact_write_set=("/etc/shadow",),
    )
    valid, err_msg = validate_delegation_constraints_upper_bound(
        constraints,
        parent_write_roots=["/tmp/project/workspace"],
    )
    assert valid is False
    assert "DELEGATION_SCOPE_EXCEEDS_PARENT" in err_msg
    assert "/etc/shadow" in err_msg


def test_upper_bound_output_dir_outside_parent_roots_fails():
    """Child allowed_output_dirs outside parent write roots fails with DELEGATION_SCOPE_EXCEEDS_PARENT."""
    constraints = StructuredDelegationConstraints(
        allowed_output_dirs=("/var/log/my_agent",),
    )
    valid, err_msg = validate_delegation_constraints_upper_bound(
        constraints,
        parent_write_roots=["/tmp/project/workspace"],
    )
    assert valid is False
    assert "DELEGATION_SCOPE_EXCEEDS_PARENT" in err_msg


def test_upper_bound_child_cannot_widen_parent_descendant_ban():
    """Child cannot set allow_descendants=True when parent has allow_descendants=False."""
    parent_c = StructuredDelegationConstraints(allow_descendants=False)
    child_c = StructuredDelegationConstraints(allow_descendants=True)
    valid, err_msg = validate_delegation_constraints_upper_bound(
        child_c,
        parent_constraints=parent_c,
    )
    assert valid is False
    assert "DELEGATION_SCOPE_EXCEEDS_PARENT" in err_msg


def test_create_task_attributes_raises_on_unknown_native_action():
    """When native actor has unrecognized prohibited_actions, create_task_attributes raises DelegationConstraintError."""
    raw = {
        "delegation_constraints": {
            "actor_runtime": "native",
            "prohibited_actions": ["quantum_warp_drive"],
        }
    }
    with pytest.raises(DelegationConstraintError) as exc_info:
        create_task_attributes(raw)
    assert "Unknown prohibited action tokens" in str(exc_info.value)
    assert exc_info.value.error_code == "TOOL_INVALID_ARGUMENTS"


# -----------------------------------------------------------------------------
# 5. Exact write-set & write boundary (blocking unlisted sibling files)
# -----------------------------------------------------------------------------


def test_exact_write_set_allows_listed_file_blocks_unlisted_sibling(tmp_path):
    """exact_write_set: listed file is allowed; sibling file in the exact same folder is BLOCKED."""
    ws = (tmp_path / "workspace").resolve()
    ws.mkdir(parents=True)
    file_a = (ws / "module_a.py").resolve()
    file_b = (ws / "module_b.py").resolve()
    file_a.write_text("print('a')", encoding="utf-8")
    file_b.write_text("print('b')", encoding="utf-8")

    # With exact_write_set naming only file_a
    params = {
        "delegation_constraints": {
            "exact_write_set": [str(file_a)],
        }
    }
    roots = resolved_extra_write_roots(agent=None, params=params, goal="edit a")
    assert str(file_a) in roots
    assert str(ws) not in roots  # Broad directory must NOT be included!

    boundary = {
        "allowed_write_roots": roots,
        "task_dir": str(tmp_path / "task_internal"),
    }

    # Writing to file_a must be allowed
    err_a = validate_write_boundary(
        "write_file",
        {"path": str(file_a), "content": "updated a"},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert err_a == ""

    # Writing to sibling file_b in the same folder MUST BE BLOCKED
    err_b = validate_write_boundary(
        "write_file",
        {"path": str(file_b), "content": "updated b"},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert "写入被阻止: 目标路径不在 allowed_write_roots 内" in err_b


def test_allowed_output_dirs_allows_writing_inside_dir(tmp_path):
    """allowed_output_dirs: writing inside output dir is allowed, writing outside is blocked."""
    ws = (tmp_path / "workspace").resolve()
    ws.mkdir(parents=True)
    output_dir = (ws / "output").resolve()
    output_dir.mkdir()
    other_dir = (ws / "secret").resolve()
    other_dir.mkdir()

    params = {
        "delegation_constraints": {
            "allowed_output_dirs": [str(output_dir)],
        }
    }
    roots = resolved_extra_write_roots(agent=None, params=params, goal="generate artifacts")
    assert str(output_dir) in roots

    boundary = {
        "allowed_write_roots": roots,
        "task_dir": str(tmp_path / "task_internal"),
    }

    # Writing inside output_dir is allowed
    err_out = validate_write_boundary(
        "write_file",
        {"path": str(output_dir / "report.md"), "content": "data"},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert err_out == ""

    # Writing inside other_dir is blocked
    err_other = validate_write_boundary(
        "write_file",
        {"path": str(other_dir / "leak.txt"), "content": "data"},
        workspace_root=ws,
        write_boundary=boundary,
    )
    assert "写入被阻止: 目标路径不在 allowed_write_roots 内" in err_other


def test_write_policy_requested_child_write_roots_isolates_parent_when_exact_set(tmp_path):
    """write_policy: requested_child_write_roots does not inherit parent workspace when exact_write_set is specified."""
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(
        goal="parent",
        thought="t",
        plan=["p"],
    )
    parent.allowed_write_roots = [str(tmp_path / "parent_ws")]
    manager.save(parent)

    exact_file = str(tmp_path / "parent_ws" / "exact.py")
    req = ChildWriteRootRequest(
        parent=parent,
        spec_goal="fix exact",
        explicit_roots=[exact_file],
    )
    child_roots = requested_child_write_roots(req)
    assert child_roots == [exact_file]
    assert str(tmp_path / "parent_ws") not in child_roots


# -----------------------------------------------------------------------------
# 6. allowed_read_inputs and ChildReadScope
# -----------------------------------------------------------------------------


def test_child_read_scope_enforces_allowed_read_inputs(tmp_path):
    """ChildReadScope: check() allows paths in allowed_read_inputs, blocks unlisted inputs."""
    owner_root = (tmp_path / "owner").resolve()
    owner_root.mkdir()
    allowed_file = (owner_root / "input.txt").resolve()
    allowed_file.write_text("hello", encoding="utf-8")
    unlisted_file = (owner_root / "private.txt").resolve()
    unlisted_file.write_text("secret", encoding="utf-8")

    policy = PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(owner_root))
    scope = ChildReadScope(
        owner_root=owner_root,
        cwd=owner_root,
        granted_roots=(owner_root,),
        policy=policy,
        allowed_read_inputs=(allowed_file,),
    )

    # Allowed input path checks OK
    _, decision_a = scope.check(str(allowed_file))
    assert decision_a.allowed is True

    # Unlisted input path in same workspace is blocked by allowed_read_inputs constraint
    _, decision_b = scope.check(str(unlisted_file))
    assert decision_b.allowed is False
    assert decision_b.code == "PATH_NOT_IN_ALLOWED_READ_INPUTS"


# -----------------------------------------------------------------------------
# 7. permitted_tools & prohibited_actions enforcement
# -----------------------------------------------------------------------------


def test_scheduled_child_tools_narrows_to_permitted_tools():
    """scheduled_child_tools narrows candidate tools strictly to permitted_tools."""
    spec = HierarchyChildSpec(
        goal="read only child",
        attributes={
            "delegation_constraints": {
                "permitted_tools": ["read_file", "search_text"],
            }
        },
    )
    req = ToolPolicyRequest(
        parent_tools=["read_file", "search_text", "write_file", "edit_file"],
        spec=spec,
        extra_write_roots=[],
        role="worker",
    )
    child_tools = scheduled_child_tools(req)
    assert "read_file" in child_tools
    assert "search_text" in child_tools
    assert "write_file" not in child_tools
    assert "edit_file" not in child_tools


def test_scheduled_child_tools_removes_prohibited_actions():
    """scheduled_child_tools removes spawn_children and network tools when prohibited."""
    spec = HierarchyChildSpec(
        goal="offline leaf",
        attributes={
            "delegation_constraints": {
                "prohibited_actions": ["spawn_children", "network_access"],
            }
        },
    )
    req = ToolPolicyRequest(
        parent_tools=["read_file", "create_subagents", "web_search", "write_file"],
        spec=spec,
        extra_write_roots=[],
        role="coordinator",
    )
    child_tools = scheduled_child_tools(req)
    assert "create_subagents" not in child_tools
    assert "web_search" not in child_tools
    assert "read_file" in child_tools


# -----------------------------------------------------------------------------
# 7. Regression Tests (a)-(f) for Rework SB-R3
# -----------------------------------------------------------------------------


def test_orch06_f_from_dict_rejects_bool_and_negative_integers():
    """StructuredDelegationConstraints.from_dict rejects bool and negative integers."""
    c_bool = StructuredDelegationConstraints.from_dict(
        {"max_cumulative_children": True, "max_descendant_depth": False}
    )
    assert c_bool.max_cumulative_children is None
    assert c_bool.max_descendant_depth is None

    c_neg = StructuredDelegationConstraints.from_dict(
        {"max_cumulative_children": -1, "max_descendant_depth": -5}
    )
    assert c_neg.max_cumulative_children is None
    assert c_neg.max_descendant_depth is None

    c_valid = StructuredDelegationConstraints.from_dict(
        {"max_cumulative_children": 3, "max_descendant_depth": 0}
    )
    assert c_valid.max_cumulative_children == 3
    assert c_valid.max_descendant_depth == 0


def _make_test_tool_call(snapshot, tool_name: str, arguments: dict) -> ToolCall:
    runtime = snapshot.runtime(tool_name)
    schema_hash = runtime.model_spec.schema_hash if runtime is not None else "sha256:" + ("0" * 64)
    return ToolCall(
        call_id="call-test",
        tool_name=tool_name,
        arguments=arguments,
        source_protocol="native",
        schema_hash=schema_hash,
        run_id="run-test-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
    )


def test_orch06_a_action_policy_allows_create_subagents_constraints_and_rejects_unknown(tmp_path):
    """ActionPolicy().decide allows items[0].delegation_constraints and rejects unknown keys."""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    snapshot = agent.tools.runtime_snapshot()

    # Allowed valid delegation_constraints
    call_valid = _make_test_tool_call(
        snapshot,
        "create_subagents",
        {
            "items": [
                {
                    "goal": "do something safely",
                    "delegation_constraints": {
                        "max_cumulative_children": 2,
                        "allow_descendants": True,
                        "prohibited_actions": ["git_push"],
                    },
                }
            ]
        },
    )
    dec_valid = ActionPolicy().decide(ActionPolicyRequest(call_valid, snapshot, tmp_path, approval_mode="auto"))
    assert dec_valid.status == "allow"

    # Rejected unknown keys inside delegation_constraints
    call_invalid = _make_test_tool_call(
        snapshot,
        "create_subagents",
        {
            "items": [
                {
                    "goal": "bad constraint",
                    "delegation_constraints": {
                        "unknown_property_xyz": "not_allowed",
                    },
                }
            ]
        },
    )
    dec_invalid = ActionPolicy().decide(ActionPolicyRequest(call_invalid, snapshot, tmp_path, approval_mode="auto"))
    assert dec_invalid.status == "deny"
    assert "TOOL_INVALID_ARGUMENTS" in dec_invalid.reason_codes


def test_orch06_b_runtimedb_delegations_fk_join_and_count(tmp_path):
    """SubAgentManager + RuntimeRepository.record_run_creation persists granted_scope_json and counts via FK join."""
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        _persist_delegation_scope,
    )

    repo = RuntimeRepository(tmp_path / "runtime.db")
    manager = SubAgentManager(tmp_path)
    manager.runtime_db = repo
    manager.owner_id = "owner-1"

    parent_task = manager.create_run(
        goal="parent task",
        thought="parent plan",
        plan=["step1"],
    )
    parent_task.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=5
    ).to_dict()
    manager.save(parent_task)

    child_task = manager.create_run(
        goal="child task",
        thought="child plan",
        plan=["step1"],
        parent_id=parent_task.id,
    )
    child_task.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=2,
        prohibited_actions=("git_push",),
    ).to_dict()
    manager.save(child_task)

    # Update granted_scope_json via _persist_delegation_scope (matches child_agent_run_id via agent_runs FK join)
    _persist_delegation_scope(manager, child_task)

    # Verify delegation row has updated granted_scope_json
    with repo._runtime_connection() as conn:
        row = conn.execute(
            """SELECT d.granted_scope_json FROM delegations d
            LEFT JOIN agent_runs p ON p.agent_run_id = d.parent_agent_run_id
            WHERE p.run_id = ? OR d.parent_agent_run_id = ?""",
            (parent_task.id, parent_task.id),
        ).fetchone()
        assert row is not None
        saved_scope = json.loads(row[0])
        assert saved_scope["max_cumulative_children"] == 2
        assert "git_push" in saved_scope["prohibited_actions"]

    # Verify cumulative_created_child_count counts via FK join on agent_runs.run_id
    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=repo,
        _current_subagent_run_id=parent_task.id,
    )
    assert cumulative_created_child_count(agent, parent_task.id) == 1


def test_orch06_c_child_write_boundary_enforces_allowed_read_inputs(tmp_path):
    """Child write_boundary sets read_scope_mode='exact' and exact_read_boundary_error enforces allowed_read_inputs."""
    from agent_py_agent.agent.tooling.runtime_boundary import exact_read_boundary_error

    manager = SubAgentManager(tmp_path)
    allowed_file = tmp_path / "allowed.txt"
    allowed_file.write_text("allowed data", encoding="utf-8")
    blocked_sibling = tmp_path / "sibling_secret.txt"
    blocked_sibling.write_text("secret data", encoding="utf-8")

    task = manager.create_run(
        goal="read only allowed",
        thought="plan",
        plan=["p"],
    )
    task.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        actor_runtime="native",
        allowed_read_inputs=(str(allowed_file),),
    ).to_dict()
    manager.save(task)

    boundary = manager.runner_context._build_write_boundary(task)
    assert boundary["read_scope_mode"] == "exact"

    # Reading allowed_file succeeds (no boundary error)
    err_allowed = exact_read_boundary_error(
        "read_file",
        {"path": str(allowed_file)},
        workspace_root=tmp_path,
        write_boundary=boundary,
    )
    assert err_allowed == ""

    # Reading unlisted sibling file is blocked with boundary error
    err_blocked = exact_read_boundary_error(
        "read_file",
        {"path": str(blocked_sibling)},
        workspace_root=tmp_path,
        write_boundary=boundary,
    )
    assert err_blocked != ""


def test_orch06_d_action_policy_blocks_prohibited_actions(tmp_path):
    """ActionPolicy blocks git push, rm, and run_command when prohibited_actions are set."""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    snapshot = agent.tools.runtime_snapshot()

    # 1. Block git_push command
    boundary_push = {"prohibited_actions": ["git_push"]}
    call_push = _make_test_tool_call(
        snapshot,
        "run_command",
        {"command": "git push origin main"},
    )
    dec_push = ActionPolicy().decide(ActionPolicyRequest(call_push, snapshot, tmp_path, write_boundary=boundary_push, approval_mode="auto"))
    assert dec_push.status == "deny"
    assert "TOOL_PERMISSION_DENIED" in dec_push.reason_codes

    # 2. Block file_delete (rm command)
    boundary_rm = {"prohibited_actions": ["file_delete"]}
    call_rm = _make_test_tool_call(
        snapshot,
        "run_command",
        {"command": "rm some_file.txt"},
    )
    dec_rm = ActionPolicy().decide(ActionPolicyRequest(call_rm, snapshot, tmp_path, write_boundary=boundary_rm, approval_mode="auto"))
    assert dec_rm.status == "deny"
    assert "TOOL_PERMISSION_DENIED" in dec_rm.reason_codes

    # 3. Block shell_execution (all run_command blocked)
    boundary_shell = {"prohibited_actions": ["shell_execution"]}
    call_shell = _make_test_tool_call(
        snapshot,
        "run_command",
        {"command": "echo safe"},
    )
    dec_shell = ActionPolicy().decide(ActionPolicyRequest(call_shell, snapshot, tmp_path, write_boundary=boundary_shell, approval_mode="auto"))
    assert dec_shell.status == "deny"
    assert "TOOL_PERMISSION_DENIED" in dec_shell.reason_codes

    # 4. Block apply_patch with delete file
    call_patch_del = _make_test_tool_call(
        snapshot,
        "apply_patch",
        {"patch": "*** Delete File: unwanted.py\n"},
    )
    dec_patch = ActionPolicy().decide(ActionPolicyRequest(call_patch_del, snapshot, tmp_path, write_boundary=boundary_rm, approval_mode="auto"))
    assert dec_patch.status == "deny"
    assert "TOOL_PERMISSION_DENIED" in dec_patch.reason_codes


def test_orch06_e_grandchild_inherits_parent_constraints(tmp_path):
    """Grandchild creation without explicit delegation_constraints inherits parent constraints."""
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        _bind_delegation_constraints,
    )

    manager = SubAgentManager(tmp_path)
    parent_c = StructuredDelegationConstraints(
        max_cumulative_children=4,
        allow_descendants=True,
        max_descendant_depth=2,
        prohibited_actions=("git_push", "shell_execution"),
    )
    parent_task = manager.create_run(
        goal="parent coordinator",
        thought="plan",
        plan=["p"],
    )
    parent_task.attributes["delegation_constraints"] = parent_c.to_dict()
    manager.save(parent_task)

    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id=parent_task.id,
        _current_run_params=RunParams(task_attributes=parent_task.attributes, host_channel_actor=None),
        tools=None,
    )
    set_current_subagent_context(agent, run_id=parent_task.id, task_attributes=parent_task.attributes)

    # Grandchild params without explicit delegation_constraints
    grandchild_attrs: dict[str, object] = {}
    _bind_delegation_constraints(grandchild_attrs, raw_params={}, agent=agent)

    assert "delegation_constraints" in grandchild_attrs
    inherited = StructuredDelegationConstraints.from_dict(grandchild_attrs["delegation_constraints"])
    assert inherited.max_cumulative_children == 4
    # max_descendant_depth is decremented from 2 to 1
    assert inherited.max_descendant_depth == 1
    assert "git_push" in inherited.prohibited_actions
    assert "shell_execution" in inherited.prohibited_actions


def test_orch06_grandchild_monotonic_write_and_tool_scope_enforcement(tmp_path: Path):
    """End-to-end unmocked SimpleAgent + CreateSubagentsTool grandchild monotonic scope enforcement."""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

    home_dir = tmp_path / "home"
    work_dir = tmp_path / "workspace"
    home_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    allowed_file = work_dir / "allowed.py"
    allowed_file.touch()
    forbidden_file = work_dir / "forbidden.py"
    forbidden_file.touch()

    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home_dir),
        execution_mode="local_unmanaged",
    )
    agent = SimpleAgent(config, work_dir)

    parent_constraints = StructuredDelegationConstraints(
        exact_write_set=(str(allowed_file),),
        permitted_tools=("read_file", "write_file", "create_subagents"),
        allowed_read_inputs=(str(allowed_file),),
        max_cumulative_children=4,
        allow_descendants=True,
        max_descendant_depth=2,
    )
    parent_task = agent.subagents.create_run(
        goal="constrained parent",
        thought="plan",
        plan=["step1"],
        role="coordinator",
        allowed_tools=["read_file", "write_file", "create_subagents"],
    )
    parent_task.extra_write_roots = [str(allowed_file)]
    parent_task.allowed_write_roots = [str(allowed_file)]
    # 独立工具权限在创建时给出；改raw历史并集不能冒充新的授权来源。
    parent_task.attributes["delegation_constraints"] = parent_constraints.to_dict()
    agent.subagents.save(parent_task)

    agent._current_run_params = RunParams(
        task_attributes=parent_task.attributes,
        host_channel_actor=None,
    )
    set_current_subagent_context(agent, run_id=parent_task.id, task_attributes=parent_task.attributes)
    tool = CreateSubagentsTool(agent)

    # 1. Constrained parent spawning grandchild with omitted delegation_constraints:
    # grandchild allowed_write_roots stays narrowed to allowed.py (validate_write_boundary blocks forbidden.py)
    out1 = tool.execute({
        "items": [{"goal": "grandchild with omitted constraints", "role": "worker"}],
    })
    assert out1.ok is True, out1.output
    grandchild_id1 = json.loads(out1.output)["created_run_ids"][0]
    grandchild1 = agent.subagents.load(grandchild_id1)
    assert grandchild1 is not None
    assert str(allowed_file) in grandchild1.allowed_write_roots

    boundary1 = {
        "allowed_write_roots": grandchild1.allowed_write_roots,
    }
    err_allowed = validate_write_boundary(
        "write_file",
        {"path": str(allowed_file), "content": "ok"},
        workspace_root=work_dir,
        write_boundary=boundary1,
    )
    assert err_allowed == ""

    err_forbidden = validate_write_boundary(
        "write_file",
        {"path": str(forbidden_file), "content": "no"},
        workspace_root=work_dir,
        write_boundary=boundary1,
    )
    assert err_forbidden != ""

    # 2. Constrained parent spawning grandchild with exact_write_set=[forbidden.py]:
    # rejected fail-closed (ok is False, effect_outcome == "not_started", DELEGATION_SCOPE_EXCEEDS_PARENT)
    out2 = tool.execute({
        "items": [
            {
                "goal": "grandchild exceeding write set",
                "role": "worker",
                "delegation_constraints": {
                    "exact_write_set": [str(forbidden_file)],
                },
            }
        ],
    })
    assert out2.ok is False
    assert out2.effect_outcome == "not_started"
    assert "DELEGATION_SCOPE_EXCEEDS_PARENT" in (out2.reported_error_code or out2.output)

    # 3. Constrained parent spawning grandchild with permitted_tools=["read_file", "run_command"]:
    # rejected fail-closed (ok is False, effect_outcome == "not_started", TOOL_PERMISSION_DENIED, no uncaught PermissionError)
    out3 = tool.execute({
        "items": [
            {
                "goal": "grandchild exceeding tool permissions",
                "role": "worker",
                "delegation_constraints": {
                    "permitted_tools": ["read_file", "run_command"],
                },
            }
        ],
    })
    assert out3.ok is False
    assert out3.effect_outcome == "not_started"
    assert "TOOL_PERMISSION_DENIED" in (out3.reported_error_code or out3.output)

    # 4. Constrained parent spawning grandchild with partial tightening {"prohibited_actions": ["git_push"]}:
    # succeeds while preserving parent's exact_write_set, allowed_read_inputs, permitted_tools, and max_cumulative_children
    out4 = tool.execute({
        "items": [
            {
                "goal": "grandchild with partial tightening",
                "role": "worker",
                "delegation_constraints": {
                    "prohibited_actions": ["git_push"],
                },
            }
        ],
    })
    assert out4.ok is True, out4.output
    grandchild_id4 = json.loads(out4.output)["created_run_ids"][0]
    grandchild4 = agent.subagents.load(grandchild_id4)
    assert grandchild4 is not None
    grandchild_c4 = StructuredDelegationConstraints.from_dict(grandchild4.attributes["delegation_constraints"])
    assert grandchild_c4.exact_write_set == parent_constraints.exact_write_set
    assert grandchild_c4.allowed_read_inputs == parent_constraints.allowed_read_inputs
    assert grandchild_c4.permitted_tools == parent_constraints.permitted_tools
    assert grandchild_c4.max_cumulative_children == parent_constraints.max_cumulative_children
    assert "git_push" in grandchild_c4.prohibited_actions


@pytest.mark.parametrize("absolute_depth", [0, 7])
def test_delegation_remaining_depth_is_consumed_by_the_same_created_chain(tmp_path, absolute_depth):
    """真实创建出的同一代约束继续进原派工门，余量2→1→0且不能显式放宽。"""
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   execution_mode="local_unmanaged"), tmp_path / "workspace")
    try:
        parent = agent.subagents.create_run(goal="parent", thought="plan", plan=["delegate"],
                                            role="coordinator", allowed_tools=["create_subagents", "read_file"])
        parent.depth = absolute_depth
        parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
            allow_descendants=True, max_cumulative_children=4, max_descendant_depth=2,
        ).to_dict()
        agent.subagents.save(parent)
        tool = CreateSubagentsTool(agent)

        def enter(task):
            agent._current_run_params = RunParams(task_attributes=task.attributes, host_channel_actor=None)
            set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)

        enter(parent)
        first = tool.execute({"items": [{"goal": "first", "role": "coordinator"}]})
        assert first.ok, first.output
        child = agent.subagents.load(json.loads(first.output)["created_run_ids"][0])
        assert child.depth == absolute_depth + 1
        assert child.attributes["delegation_constraints"]["max_descendant_depth"] == 1
        enter(child)
        for proposed in (1, 2):
            widened = tool.execute({"items": [{"goal": "widened", "role": "coordinator",
                "delegation_constraints": {"max_descendant_depth": proposed}}]})
            assert not widened.ok
            assert widened.reported_error_code == "DELEGATION_SCOPE_EXCEEDS_PARENT"
            assert not agent.subagents.load(child.id).child_ids
        second = tool.execute({"items": [{"goal": "second", "role": "coordinator"}]})
        assert second.ok, second.output
        grandchild = agent.subagents.load(json.loads(second.output)["created_run_ids"][0])
        assert grandchild.depth == absolute_depth + 2
        assert grandchild.attributes["delegation_constraints"]["max_descendant_depth"] == 0
        enter(grandchild)
        refused = tool.execute({"items": [{"goal": "third", "role": "worker"}]})
        assert not refused.ok and refused.reported_error_code == "SUBAGENT_DESCENDANTS_FORBIDDEN"
        assert refused.effect_outcome == "not_started"
        payload = json.loads(refused.output)
        assert payload["reasons"] == ["max_descendant_depth=0 (remaining generations exhausted)"]
        assert not agent.subagents.load(grandchild.id).child_ids
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            close()
