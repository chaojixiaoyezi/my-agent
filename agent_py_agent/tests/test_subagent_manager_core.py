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
