# 模块用途: create_subagents 输入预检的 visible_roots 只列子代理真实可读的位置：墙外的 allowed_read_inputs
#   不再被当成可见根回给父级；恢复建议写明复制到 visible_roots 内。可见性裁决本身不放宽。只用 tmp 隔离 home。
from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration.create_read_scope import ChildReadScope
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.tests.test_create_subagents_input_read_scope import (
    _agent,
    _create,
    _outside_worktree,
)


def _scope(tmp_path, allowed):
    owner_root = (tmp_path / "owner").resolve()
    owner_root.mkdir(exist_ok=True)
    policy = PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(owner_root))
    return owner_root, ChildReadScope(owner_root=owner_root, cwd=owner_root, granted_roots=(),
                                      policy=policy, allowed_read_inputs=tuple(allowed))


def test_unreadable_constraint_paths_are_not_reported_as_visible(tmp_path):
    outside = (tmp_path / "ref-repos" / "lib").resolve()
    owner_root, scope = _scope(tmp_path, [outside])
    assert scope.check(str(outside / "a.go"))[1].allowed is False
    assert str(outside) not in scope.visible_roots() and str(owner_root) in scope.visible_roots()
    inside = owner_root / "inputs"
    _, narrowed = _scope(tmp_path, [inside, outside])
    # 约束里可读的部分仍是唯一可见范围，不因真实根而放宽。
    assert narrowed.visible_roots() == [str(inside)]


def test_create_receipt_points_at_real_roots_for_outside_constraints(tmp_path, monkeypatch):
    agent, owner_home = _agent(tmp_path, monkeypatch)
    outside = _outside_worktree(tmp_path)
    result = _create(agent, {"items": [{"goal": "只读调研墙外资料", "input_refs": [str(outside)],
                                        "delegation_constraints": {"allowed_read_inputs": [str(outside.parent)]}}]})
    assert result.ok is False and result.effect_outcome == "not_started"
    child = json.loads(result.output)["invisible_inputs"][0]
    assert str(outside.parent.resolve()) not in child["visible_roots"]
    assert str(owner_home) in child["visible_roots"]
    assert agent.subagents.list_runs() == []


def test_path_hints_name_visible_roots_and_parent_copy():
    assert "visible_roots" in error_contract("SUBAGENT_INPUT_PATH_NOT_VISIBLE").recovery_hint
    owner_hint = error_contract("PATH_OWNER_SCOPE_BLOCKED").recovery_hint
    assert owner_hint.startswith("当前 owner 只能访问自己的目录和 shared 公共区") and "回报父级" in owner_hint
