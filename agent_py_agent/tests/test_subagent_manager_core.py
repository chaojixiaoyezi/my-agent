from __future__ import annotations

import json
from pathlib import Path


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
