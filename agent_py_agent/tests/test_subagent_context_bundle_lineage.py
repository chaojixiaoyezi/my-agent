from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_context_bundle_is_mirrored_into_agent_run_workspace(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="把 context bundle 放进运行工作位",
        thought="接管代理应该从 agent run workspace 找到它。",
        plan=["写 bundle", "检查 refs"],
    )
    task.acceptance_checks = ["agent workspace 有 context_bundle.json"]
    manager.save(task)

    context = manager.runner_context.write_execution_context(task.id)
    agent_workspace = Path(context.context_bundle["workspace_refs"]["agent_work_dir"])
    workspace_json = agent_workspace / "context_bundle.json"
    workspace_md = agent_workspace / "CONTEXT_BUNDLE.md"
    execution_context_md = Path(context.execution_context_file).read_text(encoding="utf-8")

    assert workspace_json.exists()
    assert workspace_md.exists()
    assert json.loads(workspace_json.read_text(encoding="utf-8"))["run_id"] == task.id
    assert "## Context Bundle" in execution_context_md
    assert str(workspace_json) in execution_context_md
    assert "Context Gate: PASS" in execution_context_md


def test_context_bundle_exposes_owner_workspace_separately_from_task_root(
    tmp_path,
) -> None:
    """子代理必须同时拿到稳定 owner workspace 和当前 task root，不能靠 goal 猜路径。"""
    owner_home = tmp_path / "owners" / "user-a"
    manager = SubAgentManager(
        tmp_path / "subagents",
        workspace_root=owner_home,
        workspace_roots=[owner_home],
        owner_home_dir=str(owner_home),
    )
    task = manager.create_run(
        goal="读取 workspace/input/reference_repos/project-a 并写审计报告",
        thought="复用 owner 工作区里的输入。",
        plan=["读源码", "写报告"],
    )
    task.effective_permissions = {
        "owner_home": str(owner_home),
        "owner_id": "user-a",
        "shell_access_mode": "workspace-write",
    }
    manager.save(task)

    context = manager.runner_context.write_execution_context(task.id)
    refs = context.context_bundle["workspace_refs"]

    assert refs["owner_workspace_dir"] == str(owner_home)
    assert refs["task_root"] == task.task_workspace_dir
    assert context.write_boundary["owner_workspace_dir"] == str(
        owner_home
    )
    assert str(owner_home) in Path(
        context.execution_context_file
    ).read_text(encoding="utf-8")


def test_context_bundle_records_multilevel_lineage_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    root, child, grandchild, great_grandchild = _create_context_bundle_hierarchy(manager)
    payloads = _write_and_load_context_bundle_payloads(manager, [root, child, grandchild, great_grandchild])

    assert payloads[root.id]["lineage"]["depth"] == 0
    assert payloads[root.id]["lineage"]["parent_context_bundle_ref"] == ""
    for task, parent in [(child, root), (grandchild, child), (great_grandchild, grandchild)]:
        _assert_context_bundle_lineage(payloads[task.id], task, parent, root.id)


def _create_context_bundle_hierarchy(manager: SubAgentManager):
    root = manager.create_run(
        goal="根代理拆示例网站任务",
        thought="负责拆分和汇总。",
        plan=["拆任务", "看状态"],
        acceptance_checks=["所有子树有交接包"],
    )
    child = manager.create_run(
        goal="子代理负责账号链路",
        thought="子代理要继续拆分。",
        plan=["拆账号模块", "汇总孙代理结果"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        acceptance_checks=["孙代理交付注册和登录"],
    )
    grandchild = manager.create_run(
        goal="孙代理负责注册页",
        thought="孙代理要继续拆叶子任务。",
        plan=["拆 UI", "拆测试"],
        parent_id=child.id,
        root_id=root.id,
        depth=2,
        acceptance_checks=["孙孙代理交付注册 UI 和测试"],
    )
    great_grandchild = manager.create_run(
        goal="孙孙代理实现注册表单校验",
        thought="叶子节点只做一个具体实现。",
        plan=["改代码", "跑测试"],
        parent_id=grandchild.id,
        root_id=root.id,
        depth=3,
        acceptance_checks=["注册表单错误提示可见"],
    )
    return root, child, grandchild, great_grandchild


def _write_and_load_context_bundle_payloads(manager: SubAgentManager, tasks) -> dict[str, dict[str, object]]:
    contexts = {task.id: manager.runner_context.write_execution_context(task.id) for task in tasks}
    return {
        run_id: json.loads(Path(context.context_bundle_json).read_text(encoding="utf-8"))
        for run_id, context in contexts.items()
    }


def _assert_context_bundle_lineage(payload: dict[str, object], task, parent, root_id: str) -> None:
    lineage = payload["lineage"]
    parent_ref = lineage["parent_context_bundle_ref"]
    assert lineage["root_id"] == root_id
    assert lineage["parent_id"] == parent.id
    assert lineage["depth"] == task.depth
    assert parent_ref == str(Path(parent.agent_run_workspace_dir) / "context_bundle.json")
    assert Path(parent_ref).exists()
    assert payload["gate"]["ok"] is True
