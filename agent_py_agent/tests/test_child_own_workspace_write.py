"""w3：宿主分配的自身运行目录不能在运行时被宽 owner 根吞掉。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.runner_context_service import (
    SubAgentRunnerContextService,
)
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


@pytest.fixture
def child_scope(tmp_path):
    owner = tmp_path / "ma-home" / "owners" / "local" / "main"
    task_root = owner / "runs" / "2026-10-09" / "parent-run"
    own = task_root / "work" / "agents" / "subagent-own"
    own.mkdir(parents=True)
    other = owner / "other-work"
    task = SubAgentTask(
        id="subagent-own", goal="验证工作目录", thought="", plan=[], agent_name="w3",
        role="researcher", created_at=1.0, updated_at=1.0,
        task_workspace_dir=str(task_root), agent_run_workspace_dir=str(own),
        allowed_write_roots=[str(own)], forbidden_write_roots=[str(other)],
        effective_permissions={"owner_home": str(owner)},
    )
    manager = SimpleNamespace(workspace_root=owner, owner_scope_root=owner)
    raw = SubAgentRunnerContextService(manager)._build_write_boundary(task)
    agent = SimpleNamespace(
        config=AgentConfig(), tools=SimpleNamespace(owner_scope_root=owner), subagents=manager,
        home_paths=SimpleNamespace(owner_home_dir=owner, owner_runs_dir=owner / "runs",
                                   owner_tasks_dir=owner / "tasks"),
    )
    params = SimpleNamespace(write_boundary=raw, context_scope="task_local", run_id=task.id,
                             task_attributes=runtime_task_attributes(task))
    return SimpleNamespace(owner=owner, own=own, other=other, task_root=task_root,
                           raw=raw, agent=agent, params=params)


def _write(scope, target):
    boundary = write_boundary_with_runtime_ledger(scope.agent, scope.params)
    return execute_canonical_test_call(
        scope.owner, tools={"write_file": WriteFileTool(scope.owner)}, tool_name="write_file",
        arguments={"path": str(target), "content": "w3 scratch\n"},
        write_boundary=boundary, owner_scope_root=str(scope.owner),
    ).result


def test_child_own_workspace_write_reaches_real_handler(child_scope):
    scope = child_scope
    target = scope.own / "probe-write.md"
    assert str(scope.own) in scope.raw["allowed_write_roots"]
    # 原始授权能穿过祖先 runs/ 保护；故障发生在 runtime ledger 合并时，而非路径策略。
    original = {**scope.raw, "forbidden_write_roots": [str(scope.owner / "runs")]}
    assert validate_write_boundary("write_file", {"path": str(target)},
                                   workspace_root=scope.owner, write_boundary=original) == ""
    result = _write(scope, target)
    assert result.ok, result
    assert result.handler_executed is True
    assert target.read_text(encoding="utf-8") == "w3 scratch\n"


@pytest.mark.parametrize("relative", [
    "tasks/2026-10-09/other/report.md",
    "runs/2026-10-09/other-run/work/report.md",
    "runs/2026-10-09/parent-run/work/agents/subagent-sibling/report.md",
    "runs/2026-10-09/parent-run/work/agents/subagent-own-suffix/report.md",
    "runs/2026-10-09/parent-run/checkpoint.json",
    "other-work/report.md",
])
def test_child_own_workspace_does_not_open_other_guarded_locations(child_scope, relative):
    scope = child_scope
    target = scope.owner / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("untouched", encoding="utf-8")
    result = _write(scope, target)
    assert not result.ok
    assert result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False
    assert target.read_text(encoding="utf-8") == "untouched"


def test_child_own_workspace_symlink_cannot_escape_to_sibling(child_scope):
    scope = child_scope
    sibling = scope.own.parent / "subagent-sibling"
    sibling.mkdir()
    target = sibling / "report.md"
    target.write_text("untouched", encoding="utf-8")
    (scope.own / "escape").symlink_to(sibling, target_is_directory=True)
    result = _write(scope, scope.own / "escape" / "report.md")
    assert not result.ok and result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("guard", ["forbidden_write_roots", "locked_files", "runtime_install_roots"])
def test_child_own_workspace_keeps_explicit_stronger_guards(child_scope, guard, monkeypatch):
    scope = child_scope
    target = scope.own / "guarded.md"
    if guard == "runtime_install_roots":
        # 安装根只取宿主进程事实，不能拿 boundary 的旧投影冒充当前事实。
        monkeypatch.setattr("agent_py_agent.agent.agent_core.runtime_write_guards.running_install_roots",
                            lambda: (scope.own,))
    else:
        scope.raw[guard] = [str(scope.own)]
    result = _write(scope, target)
    assert not result.ok and result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False
    assert not target.exists()


def test_child_work_dir_without_write_grant_stays_forbidden(child_scope):
    scope = child_scope
    scope.raw["allowed_write_roots"] = [str(scope.owner)]
    result = _write(scope, scope.own / "ungranted.md")
    assert not result.ok and result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False


def test_child_work_dir_without_host_identity_stays_forbidden(child_scope):
    scope = child_scope
    scope.params.task_attributes.pop("agent_run_workspace_dir")
    result = _write(scope, scope.own / "unbound.md")
    assert not result.ok and result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False


@pytest.mark.parametrize("identity", ["missing-task-dir", "mismatched-task-dir", "outside-owner", "owner-root"])
def test_child_work_dir_requires_matching_scoped_host_identity(child_scope, identity):
    scope = child_scope
    if identity == "missing-task-dir":
        scope.raw.pop("task_dir")
    elif identity == "mismatched-task-dir":
        scope.raw["task_dir"] = str(scope.own.parent / "subagent-sibling")
    else:
        claimed = scope.owner.parent / "other-owner" if identity == "outside-owner" else scope.owner
        scope.params.task_attributes["agent_run_workspace_dir"] = str(claimed)
        scope.raw["task_dir"] = str(claimed)
        scope.raw["allowed_write_roots"].append(str(claimed))
    boundary = write_boundary_with_runtime_ledger(scope.agent, scope.params)
    assert boundary["allowed_write_roots"] == [str(scope.owner)]
    result = _write(scope, scope.own / "unbound.md")
    assert not result.ok and result.error_code == "WRITE_FORBIDDEN"
    assert result.handler_executed is False


def test_child_own_workspace_keeps_task_host_state_readonly(child_scope):
    scope = child_scope
    # 即使收到更窄 allowed 根，PathAccessPolicy 的 A 类保护不能被此次修复穿透。
    protected = scope.task_root / "data" / "pack_verification"
    scope.raw["allowed_write_roots"].append(str(protected))
    result = _write(scope, protected / "record.json")
    assert not result.ok and result.error_code == "PATH_HOST_STATE_WRITE_BLOCKED"
    assert result.handler_executed is False
