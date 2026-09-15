"""工具产物账本到自然完成交接的回归；不以模型声称写了文件作为证据。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.subagents.services.runner_result_service import (
    SubAgentRunnerResultService,
)


def _task(tmp_path):
    root = tmp_path / "run" / "work" / "agents" / "child-1"
    root.mkdir(parents=True)
    return SubAgentTask(
        id="child-1", root_id="root-1", parent_id="root-1", goal="整理资料", thought="", plan=[],
        task_workspace_dir=str(tmp_path / "run"),
        agent_run_workspace_dir=str(root),
    )


def _register(task, path, **kwargs):
    return register_artifact(ArtifactRegistration(
        workspace_root=Path(task.agent_run_workspace_dir), path=path,
        run_id=kwargs.pop("run_id", task.id), task_id=task.root_id,
        created_by_tool="write_file", source="tool_result", **kwargs,
    ))


def _extract(task, parsed=None):
    return SubAgentRunnerResultService(None)._extract_parsed_output(task, parsed, 1.0, ["write_file"])


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("structured", [False, True])
def test_natural_and_structured_final_include_actual_files(tmp_path, depth, structured):
    task = _task(tmp_path)
    task.depth = depth
    task.parent_id = "parent-1" if depth == 2 else "root-1"
    product = tmp_path / "中文文件.dat"
    product.write_text("真实产物", encoding="utf-8")
    _register(task, product)
    parsed = SubAgentParsedOutput(found=True, ok=True) if structured else None

    extracted = _extract(task, parsed)

    assert [item["path"] for item in extracted.artifacts] == [str(product)]
    assert task.artifact_refs == [str(product)]
    assert task.attributes["artifact_registry_refs"][0]["path"] == str(product)
    assert task.attributes["artifact_registry_refs"][0]["created_by_tool"] == "write_file"


def test_log_archive_other_run_and_missing_file_are_not_deliverables(tmp_path):
    task = _task(tmp_path)
    product = tmp_path / "empty.weird-format"
    product.touch()
    _register(task, product)
    for name, metadata, kind, run_id in [
        ("log.txt", {"artifact_role": "tool_output_archive"}, "file", task.id),
        ("legacy-log.txt", {}, "tool_output", task.id),
        ("sibling.txt", {}, "file", "other-child"),
    ]:
        path = tmp_path / name
        path.write_text("记录", encoding="utf-8")
        _register(task, path, kind=kind, metadata=metadata, run_id=run_id)
    _register(task, tmp_path / "missing.txt")

    assert [item["path"] for item in _extract(task).artifacts] == [str(product)]


def test_latest_record_wins_and_deleted_artifact_is_removed(tmp_path):
    task = _task(tmp_path)
    product = tmp_path / "result.txt"
    product.write_text("第一版", encoding="utf-8")
    _register(task, product)
    first = _extract(task)
    product.write_text("第二版", encoding="utf-8")
    _register(task, product)
    second = _extract(task)
    assert len(second.artifacts) == 1
    assert first.artifacts[0]["registry_ref"]["sha256"] != second.artifacts[0]["registry_ref"]["sha256"]
    product.unlink()
    _register(task, product, status="deleted")
    assert _extract(task).artifacts == []
    assert task.artifact_refs == []
    assert task.attributes["artifact_registry_refs"] == []


def test_missing_registry_does_not_invent_artifact_from_prose_or_declared_path(tmp_path):
    task = _task(tmp_path)
    task.goal = "我已完成 report.md"
    task.attributes["output_files"] = [str(tmp_path / "report.md")]
    assert _extract(task).artifacts == []
    assert not (tmp_path / "report.md").exists()


def test_registry_symlink_is_reported_not_followed(tmp_path):
    task = _task(tmp_path)
    root = Path(task.agent_run_workspace_dir)
    outside = tmp_path / "private"
    outside.mkdir()
    (root / "data").symlink_to(outside, target_is_directory=True)
    assert _extract(task).artifacts == []
    assert task.attributes["artifact_registry_read_errors"]
    assert list(outside.iterdir()) == []


def test_natural_result_persists_registered_file_into_parent_handoff(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.child_result_index import child_result_index
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
        direct_children_context_payload,
    )
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
    )

    manager = SubAgentManager(workspace=tmp_path / "state", workspace_root=tmp_path)
    parent = manager.create_run(goal="统筹", thought="", plan=[], role="coordinator")
    child = manager.create_run(goal="整理数据", thought="", plan=[], parent_id=parent.id)
    product = tmp_path / "用户文件.md"
    product.write_text("真实文件", encoding="utf-8")
    _register(child, product)
    manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=child.id, dry_run=False, ok=True, message="本轮结束", response="整理已完成。",
        turn_end_reason="completed", actual_tools=["write_file"],
    ))

    loaded = manager.load(child.id)
    assert loaded.artifact_refs == [str(product)]
    assert child_result_index(None, [loaded])[0]["primary_artifact_refs"] == [str(product)]
    row = direct_children_context_payload(manager, parent.id)["items"][0]
    assert row["artifact_refs"] == [str(product)]
    assert row["completion_message"] == "整理已完成。"
