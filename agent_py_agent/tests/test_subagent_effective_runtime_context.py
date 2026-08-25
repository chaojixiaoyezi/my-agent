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
