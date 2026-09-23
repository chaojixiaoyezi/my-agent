from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.base import CreateRunParams


def test_subagent_effective_permission_snapshot_never_inherits_full_shell(tmp_path) -> None:
    """子代理可以用 shell 干活，但不会因为父级 full-access 自动拿到 full-access。"""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(
        params=CreateRunParams(
            goal="root",
            thought="coordinate",
            plan=["split"],
            parent_access_mode="full-access",
        )
    )
    child = manager.create_run(
        goal="child",
        thought="work",
        plan=["do"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    loaded_root = manager.load(root.id)
    loaded_child = manager.load(child.id)
    context = manager.runner_context.build_execution_context(child.id)

    assert loaded_root.effective_permissions["parent_access_mode"] == "full-access"
    assert loaded_root.effective_permissions["shell_access_mode"] == "workspace-write"
    assert loaded_child.effective_permissions["parent_access_mode"] == "workspace-write"
    assert loaded_child.effective_permissions["shell_access_mode"] == "workspace-write"
    assert context.effective_permissions["shell_access_mode"] == "workspace-write"
    assert context.write_boundary["shell_access_mode"] == "workspace-write"
    assert context.write_boundary["execution_cwd"] == str(manager.workspace_root)


def test_subagent_effective_permission_snapshot_keeps_restricted_shell(tmp_path) -> None:
    """父级是 restricted 时，子代理继续 restricted，不会放大成 workspace-write。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        params=CreateRunParams(
            goal="root",
            thought="work",
            plan=["do"],
            parent_access_mode="restricted",
        )
    )

    loaded = manager.load(task.id)

    assert loaded.effective_permissions["parent_access_mode"] == "restricted"
    assert loaded.effective_permissions["shell_access_mode"] == "restricted"
    assert loaded.runtime_identity.memory_namespace == f"subagent:{loaded.root_id}:{loaded.id}"


def test_owner_disabled_tool_remains_final_after_shell_dependency_closure(tmp_path) -> None:
    """旧任务会补齐续接工具，但 owner 的显式禁用不能被依赖闭包加回。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="运行一次前台检查",
        thought="只允许前台命令",
        plan=["check"],
        allowed_tools=["run_command"],
    )
    task.effective_permissions["disabled_tools"] = ["process_session"]
    manager.save(task)

    context = manager.runner_context.build_execution_context(task.id)

    assert "run_command" in context.allowed_tools
    assert "process_session" not in context.allowed_tools


def test_subagent_memory_scope_uses_create_params_policy(tmp_path) -> None:
    """子代理记忆命名空间由系统生成，保留策略来自配置/创建参数。"""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        params=CreateRunParams(
            goal="整理材料",
            thought="按父级要求处理",
            plan=["处理", "汇报"],
            memory_retention_policy="delete_after_days",
            memory_delete_after_days=3,
            destroy_summary_required=False,
        )
    )

    loaded = manager.load(task.id)
    memory_scope = loaded.attributes["memory_scope"]

    assert loaded.runtime_identity.memory_namespace == f"subagent:{loaded.root_id}:{loaded.id}"
    assert memory_scope["namespace"] == loaded.runtime_identity.memory_namespace
    assert memory_scope["retention_policy"] == "delete_after_days"
    assert memory_scope["delete_after_days"] == 3
    assert memory_scope["destroy_summary_required"] is False
    assert memory_scope["auto_promote_to_parent_memory"] is False


def test_prepared_context_projection_is_read_only_with_canonical_paths(tmp_path, monkeypatch):
    from dataclasses import asdict
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services import runner_context_service as context_service
    from agent_py_agent.tests.test_subagent_manager_core import _prepared_tree_snapshot

    manager = SubAgentManager(tmp_path)
    params = CreateRunParams(goal="检查材料", thought="只读取已授权材料", plan=["核对"],
        allowed_tools=["read_file"], context_packs=[{"summary": "材料摘要"}])
    prepared = manager.base_service.prepare_run(params=params)
    original = asdict(prepared.task)
    files = _prepared_tree_snapshot(tmp_path)
    request = manager.runner_context.prepare_execution_context(prepared.task)
    assert request.unresolved_fields == ()
    assert request.task.task_workspace_dir
    assert request.task.agent_run_workspace_dir
    assert asdict(prepared.task) == original
    with monkeypatch.context() as guard:
        guard.setattr(context_service, "time", SimpleNamespace(time=lambda: (_ for _ in ()).throw(AssertionError("纯投影不能读时间"))))
        guard.setattr(manager, "load", lambda *_: (_ for _ in ()).throw(AssertionError("纯投影不能读 manager")))
        first = context_service.project_execution_context(request)
        second = context_service.project_execution_context(request)
    assert asdict(first) == asdict(second)
    first.plan.append("不应写回")
    first.context_packs[0]["summary"] = "不应写回"
    first.write_boundary["allowed_write_roots"].append("/not-authorized")
    assert asdict(context_service.project_execution_context(request)) == asdict(second)
    assert asdict(prepared.task) == original
    assert _prepared_tree_snapshot(tmp_path) == files


def test_live_context_uses_the_same_pure_projection_after_original_commit(tmp_path, monkeypatch):
    from dataclasses import asdict
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services import runner_context_service as context_service
    from agent_py_agent.tests.test_subagent_manager_core import _prepared_tree_snapshot

    manager = SubAgentManager(tmp_path)
    params = CreateRunParams(goal="检查材料", thought="核对", plan=["检查"], allowed_tools=["read_file"])
    prepared = manager.base_service.prepare_run(params=params)
    created = manager.base_service.create_run(params=params, prepared=prepared)
    child = manager.create_run(goal="协助核对", parent_id=created.id, root_id=created.id, depth=1)
    loaded = manager.load(created.id)
    monkeypatch.setattr(context_service, "time", SimpleNamespace(time=lambda: 1234.0))
    files = _prepared_tree_snapshot(tmp_path)
    request = manager.runner_context.prepare_execution_context(loaded)
    projected = context_service.project_execution_context(request)
    live = manager.runner_context.build_execution_context(created.id)
    assert request.unresolved_fields == ()
    assert asdict(live) == asdict(projected)
    assert live.context_bundle["direct_children"]["items"][0]["run_id"] == child.id
    assert _prepared_tree_snapshot(tmp_path) == files


def test_workspace_path_projection_matches_save_when_existing_root_rejects_changed_override(tmp_path):
    from dataclasses import asdict

    from agent_py_agent.agent.subagents.services.task_workspace_adapter import (
        project_task_workspace_fields,
    )
    from agent_py_agent.tests.test_subagent_manager_core import _prepared_tree_snapshot

    manager = SubAgentManager(tmp_path / "manager")
    trusted_root = tmp_path / "task-space"
    task = manager.create_run(params=CreateRunParams(goal="核对路径", thought="保持原边界", plan=["检查"],
        attributes={"run_workspace": {"task_root": str(trusted_root)}}))
    task.attributes["run_workspace"]["task_root"] = str(tmp_path / "other-task-space")
    original = asdict(task)
    before = _prepared_tree_snapshot(tmp_path)
    projected = project_task_workspace_fields(manager.workspace, task)
    assert asdict(task) == original
    assert _prepared_tree_snapshot(tmp_path) == before
    assert projected.task_workspace_dir != task.attributes["run_workspace"]["task_root"]
    assert projected.daily_ledger_file == task.daily_ledger_file
    assert projected.daily_ledger_last_event_id == task.daily_ledger_last_event_id

    manager.save(task)
    assert projected.task_workspace_dir == task.task_workspace_dir
    assert projected.agent_run_workspace_dir == task.agent_run_workspace_dir
    assert projected.allowed_write_roots == task.allowed_write_roots
