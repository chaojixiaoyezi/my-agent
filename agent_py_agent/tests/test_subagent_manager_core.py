from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_manager_builds_standard_work_order_paths(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(workspace=tmp_path)
    paths = manager._build_work_order_paths("run_123", extra_write_roots=["/tmp/extra"])

    assert paths["task_dir"] == str(tmp_path / "run_123")
    assert paths["data_dir"] == str(tmp_path / "run_123" / "data")
    assert paths["logs_dir"] == str(tmp_path / "run_123" / "logs")
    assert str(tmp_path / "run_123") in paths["allowed_write_roots"]
    assert "/tmp/extra" in paths["allowed_write_roots"]
    assert "forbidden_write_roots" in paths


def test_manager_creates_work_order_files(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.models import SubAgentTask

    manager = SubAgentManager(workspace=tmp_path)
    task = SubAgentTask(
        id="test_json_files",
        goal="测试任务",
        thought="思考",
        plan=["步骤1"],
        agent_name="test",
        created_at=1234567890.0,
        updated_at=1234567890.0,
        **manager._build_work_order_paths("test_json_files"),
    )

    manager._ensure_work_order_files(task)

    output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    status = json.loads(Path(task.status_report_json).read_text(encoding="utf-8"))
    deps = json.loads(Path(task.dependencies_json).read_text(encoding="utf-8"))
    assert Path(task.data_dir).exists()
    assert Path(task.output_dir).exists()
    assert Path(task.logs_dir).exists()
    assert output["run_id"] == "test_json_files"
    assert status["run_id"] == "test_json_files"
    assert deps["dependencies"] == []


def test_manager_does_not_parse_write_roots_from_natural_language():
    import agent_py_agent.agent.subagents.manager as manager

    assert not hasattr(manager, "_extract_write_dirs")


def test_inherited_workspace_root_removes_only_equal_default_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    task = SubAgentTask(
        id="run_workspace_inheritance",
        goal="在继承工作区中写产品文件",
        thought="按父级权限执行",
        plan=["写入工作区"],
        owner="local/main",
        allowed_write_roots=[str(tmp_path)],
        forbidden_write_roots=[
            str(tmp_path),
            str(tmp_path / ".ssh"),
            str(tmp_path / "Downloads"),
        ],
        attributes={
            "workspace_root": str(tmp_path),
            "workspace_roots": [str(tmp_path)],
        },
    )

    assert _reconciled_forbidden_write_roots(task) == [
        str(tmp_path / ".ssh"),
        str(tmp_path / "Downloads"),
    ]


def test_non_workspace_equal_allow_does_not_remove_default_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    external = tmp_path / "external"
    task = SubAgentTask(
        id="run_external_scope",
        goal="尝试写外部路径",
        thought="守住工作区上界",
        plan=["核对路径"],
        allowed_write_roots=[str(external)],
        forbidden_write_roots=[str(external)],
        attributes={"workspace_root": str(tmp_path)},
    )

    assert _reconciled_forbidden_write_roots(task) == [str(external)]


def test_remote_owner_keeps_equal_host_home_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    task = SubAgentTask(
        id="run_remote_owner",
        goal="处理远程 owner 任务",
        thought="保留宿主围栏",
        plan=["核对权限"],
        owner="users/remote-a",
        allowed_write_roots=[str(tmp_path)],
        forbidden_write_roots=[str(tmp_path)],
        attributes={"workspace_root": str(tmp_path)},
    )

    assert _reconciled_forbidden_write_roots(task) == [str(tmp_path)]


def test_create_run_applies_inherited_workspace_reconciliation(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents import manager_work_orders
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    monkeypatch.setattr(
        manager_work_orders,
        "_default_forbidden_write_roots",
        lambda: [str(tmp_path), str(tmp_path / ".ssh")],
    )
    manager = SubAgentManager(workspace=tmp_path, workspace_root=tmp_path)

    task = manager.create_run(
        goal="在父级工作区写一个文件",
        thought="继承父级边界",
        plan=["写入", "回报"],
        owner="local/main",
        extra_write_roots=[str(tmp_path)],
    )

    assert str(tmp_path) in task.allowed_write_roots
    assert str(tmp_path) not in task.forbidden_write_roots
    assert str(tmp_path / ".ssh") in task.forbidden_write_roots


# LLM: 检查本测试临时树的目录和实际字节，不能以 mocked save 的调用次数替代无落盘证据。
# 函数用途: 记录准备或失败前后的文件树，发现隐藏的任务、线程、空目录及账本写入。
def _prepared_tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


@pytest.mark.parametrize("workspace_override", [False, True])
def test_prepared_root_child_grandchild_commit_the_same_object_and_identity(tmp_path, monkeypatch, workspace_override):
    from dataclasses import asdict

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services import base
    from agent_py_agent.agent.subagents.services.runner_context_service import (
        project_execution_context,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    manager = agent.subagents
    parent = None
    root_id = ""
    attributes = {"run_workspace": {"task_root": str(tmp_path / "task-space")}} if workspace_override else {}
    extra_root = str(tmp_path / "authorized-output")
    for depth in range(3):
        params = base.CreateRunParams(goal="核对材料", thought="按授权范围检查", plan=["检查", "汇报"],
            parent_id=parent.id if parent else "", root_id=root_id, depth=depth,
            allowed_tools=["read_file"], parent_access_mode="restricted", attributes=attributes,
            extra_write_roots=[extra_root])
        before = _prepared_tree_snapshot(tmp_path)
        prepared = manager.base_service.prepare_run(params=params)
        raw_task = asdict(prepared.task)
        request = manager.runner_context.prepare_execution_context(prepared.task)
        projected = project_execution_context(request)
        assert _prepared_tree_snapshot(tmp_path) == before
        assert asdict(prepared.task) == raw_task
        assert request.unresolved_fields == ()
        assert not Path(request.task.agent_run_workspace_dir).exists()
        assert request.task.daily_ledger_file == request.task.daily_ledger_last_event_id == ""
        assert extra_root in request.task.allowed_write_roots
        assert prepared.task.task_dir not in request.task.allowed_write_roots
        with pytest.raises(FileNotFoundError):
            manager.load(prepared.run_id)
        assert agent.conversation_store.threads.load(prepared.task.agent_thread_id) is None
        original_identity = (prepared.run_id, prepared.task.agent_thread_id, prepared.task.subagent_session_id)
        with monkeypatch.context() as guard:
            guard.setattr(base, "_framework_new_id", lambda *_: pytest.fail("提交不能再生成 run_id"))
            created = manager.base_service.create_run(params=params, prepared=prepared)
        assert created is prepared.task
        assert (created.id, created.agent_thread_id, created.subagent_session_id) == original_identity
        assert manager.load(created.id).id == prepared.run_id
        thread = agent.conversation_store.threads.load(created.agent_thread_id)
        assert thread is not None
        assert created.effective_permissions["shell_access_mode"] == "restricted"
        live = manager.runner_context.build_execution_context(created.id)
        assert projected.task_dir == live.task_dir
        assert projected.execution_context_json == live.execution_context_json
        assert projected.execution_context_file == live.execution_context_file
        assert projected.context_bundle_json == live.context_bundle_json
        assert projected.context_bundle_file == live.context_bundle_file
        assert projected.context_bundle["workspace_refs"] == live.context_bundle["workspace_refs"]
        assert projected.write_boundary == live.write_boundary
        assert request.task.task_artifact_manifest_jsonl == created.task_artifact_manifest_jsonl
        assert request.task.agent_run_artifact_manifest_jsonl == created.agent_run_artifact_manifest_jsonl
        assert created.daily_ledger_file and created.daily_ledger_last_event_id
        if parent:
            assert created.parent_subagent_session_id == parent.subagent_session_id
            assert created.root_subagent_session_id == parent.root_subagent_session_id
            assert created.id in manager.load(parent.id).child_ids
        root_id = root_id or created.id
        assert created.root_id == root_id
        parent = created
    assert len(manager.list_runs()) == 3


@pytest.mark.parametrize("changed", ["input", "identity", "task_permission", "owner_policy", "parent_revision"])
def test_prepared_run_rejects_drift_before_any_materialization(tmp_path, changed):
    from dataclasses import replace

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", parent_access_mode="restricted")
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"], parent_id=parent.id,
        root_id=parent.id, depth=1, allowed_tools=["read_file"])
    prepared = manager.base_service.prepare_run(params=params)
    if changed == "input":
        params = replace(params, allowed_tools=["write_file"])
    elif changed == "identity":
        prepared.task.id = "another-valid-run"
    elif changed == "task_permission":
        prepared.task.effective_permissions["shell_access_mode"] = "full-access"
    elif changed == "owner_policy":
        manager.owner_policy_snapshot = {"tools": {"disabled_tools": ["read_file"]}}
    else:
        manager.mutate(parent.id, lambda task: task.blockers.append("父任务已更新"))
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已变化"):
        manager.base_service.create_run(params=params, prepared=prepared)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert len(manager.list_runs()) == 1


def test_prepared_run_isolated_from_input_mutation_and_cannot_publish_twice(tmp_path):
    from copy import deepcopy

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"],
        allowed_tools=["read_file"], context_packs=[{"summary": "输入材料"}])
    prepared = manager.base_service.prepare_run(params=params)
    params.context_packs[0]["summary"] = "后来修改"
    assert prepared.params.context_packs[0]["summary"] == "输入材料"
    assert prepared.task.context_packs[0]["summary"] == "输入材料"
    unchanged = deepcopy(prepared)
    created = manager.base_service.create_run(params=prepared.params, prepared=prepared)
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已经发布"):
        manager.base_service.create_run(params=unchanged.params, prepared=unchanged)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert [task.id for task in manager.list_runs()] == [created.id]


def test_refreeze_preserves_task_identity_and_never_changes_published_task(tmp_path):
    from dataclasses import asdict, replace

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务")
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"], parent_id=parent.id,
        root_id=parent.id, attributes={"host_model_profile.v1": {"profile_id": "default"}})
    prepared = manager.base_service.prepare_run(params=params)
    manager.create_run(goal="同批先保存的孩子", parent_id=parent.id, root_id=parent.id)
    selected = replace(params, attributes={"host_model_profile.v1": {"profile_id": "selected"}})
    before = _prepared_tree_snapshot(tmp_path)
    updated = manager.base_service.refreeze_run(params=selected, prepared=prepared)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert updated.task is prepared.task
    assert (updated.run_id, updated.created_at) == (prepared.run_id, prepared.created_at)
    assert updated.parent_revision > prepared.parent_revision
    created = manager.create_run(params=selected, prepared=updated)
    original = asdict(created)
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已经发布"):
        manager.base_service.refreeze_run(params=params, prepared=updated)
    assert asdict(created) == original
    assert _prepared_tree_snapshot(tmp_path) == before
