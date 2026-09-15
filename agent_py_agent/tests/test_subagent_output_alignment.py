"""输出引用遵循真实 cwd；声明不增加权限，收口不搬运业务文件。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.subagents.context_bundle_contracts import output_contract, task_packet
from agent_py_agent.agent.subagents.context_bundle_refs import execution_cwd
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.output_alignment import anchored_output_refs
from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary


def _create(tmp_path, refs):
    owner = tmp_path / "owner"
    owner.mkdir(exist_ok=True)
    manager = SubAgentManager(workspace=tmp_path / "state", workspace_root=owner)
    task = manager.create_run(params=CreateRunParams(
        goal="实现数据整理工具", thought="", plan=["实现和验证"], role="worker",
        attributes={"output_files": refs},
        extra_write_roots=[str(owner)],
    ))
    return manager, task, owner


@pytest.mark.parametrize("ref", ["app/main.py", "output/report.md", "work/log.txt", "tasks/中文.md"])
def test_relative_output_uses_execution_cwd_without_internal_redirect(tmp_path, ref):
    manager, task, owner = _create(tmp_path, [ref])
    assert execution_cwd(task) == str(owner)
    assert anchored_output_refs(task).anchored_refs == [str(owner / ref)]
    contract = output_contract(task)
    assert contract["required_file_refs"] == [str(owner / ref)]
    assert "output_delivery_map" not in contract
    assert not (owner / ref).exists()
    manager.save(task)
    assert anchored_output_refs(manager.load(task.id)) == anchored_output_refs(task)


def test_explicit_absolute_output_is_preserved_and_does_not_grant_access(tmp_path):
    outside = tmp_path / "another-owner" / "result.md"
    manager, task, owner = _create(tmp_path, [str(outside)])
    assert anchored_output_refs(task).anchored_refs == [str(outside)]
    boundary = manager.runner_context._build_write_boundary(task)
    assert str(outside.parent) not in boundary["allowed_write_roots"]
    assert validate_write_boundary(
        "write_file", {"path": str(outside), "content": "x"}, workspace_root=owner,
        write_boundary=boundary,
    )


def test_parent_directory_reference_is_not_replaced_with_basename(tmp_path):
    _, task, owner = _create(tmp_path, ["../other/result.md"])
    assert anchored_output_refs(task).anchored_refs == [str((owner / "../other/result.md").resolve())]


def test_internal_run_output_field_does_not_override_cwd(tmp_path):
    _, task, owner = _create(tmp_path, ["report.md"])
    task.attributes["run_workspace"] = {"output_dir": str(tmp_path / "internal-output")}
    assert anchored_output_refs(task).anchored_refs == [str(owner / "report.md")]


def test_current_conversation_cwd_wins_over_stale_workspace_projection(tmp_path):
    manager, task, owner = _create(tmp_path, ["report.md"])
    project = owner / "project"
    project.mkdir()
    task.attributes["conversation_execution_cwd"] = str(project)
    assert anchored_output_refs(task).anchored_refs == [str(project / "report.md")]
    boundary = manager.runner_context._build_write_boundary(task)
    assert boundary["execution_cwd"] == str(project)


def test_missing_cwd_does_not_guess_internal_or_host_directory(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.output_alignment import (
        sanitize_self_locked_delivery_targets,
    )

    _, task, _ = _create(tmp_path, ["app/main.py"])
    task.attributes = {"output_files": ["app/main.py"]}
    task.effective_permissions = {}
    monkeypatch.chdir(tmp_path)
    task.locked_files = [str(tmp_path / "app/main.py")]
    assert anchored_output_refs(task).anchored_refs == ["app/main.py"]
    assert anchored_output_refs(task).warnings == []
    assert sanitize_self_locked_delivery_targets(task, now=1.0) == []
    assert task.locked_files == [str(tmp_path / "app/main.py")]


def test_declared_output_has_one_path_with_multiple_allowed_roots(tmp_path):
    _, task, owner = _create(tmp_path, ["app/main.py"])
    task.allowed_write_roots.append(str(tmp_path / "additional-write-root"))
    assert task_packet(task)["file_contract"]["required_file_refs"] == [str(owner / "app/main.py")]


def test_first_attempt_and_rebuilt_context_share_the_same_cwd_and_authority(tmp_path):
    manager, task, owner = _create(tmp_path, ["app/main.py"])
    first = manager.runner_context.build_execution_context(task.id)
    second = manager.runner_context.build_execution_context(task.id)
    assert first.write_boundary == second.write_boundary
    assert first.write_boundary["execution_cwd"] == str(owner)
    assert not validate_write_boundary(
        "write_file", {"path": str(owner / "app/main.py"), "content": "x"},
        workspace_root=owner, write_boundary=first.write_boundary,
    )


def test_explicit_forbidden_and_unrelated_task_lock_remain_effective(tmp_path):
    manager, task, owner = _create(tmp_path, ["app/main.py"])
    task.forbidden_write_roots = [str(owner / "private")]
    task.locked_files = [str(owner / "locked.txt")]
    manager.save(task)
    loaded = manager.load(task.id)
    boundary = manager.runner_context._build_write_boundary(loaded)
    assert boundary["forbidden_write_roots"] == loaded.forbidden_write_roots
    assert boundary["locked_files"] == loaded.locked_files
    assert validate_write_boundary(
        "write_file", {"path": str(owner / "private/x"), "content": "x"},
        workspace_root=owner, write_boundary=boundary,
    )


def test_natural_closeout_does_not_copy_a_lookalike_into_declared_target(tmp_path):
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
    )

    manager, task, owner = _create(tmp_path, ["report.md"])
    legacy = Path(task.task_workspace_dir) / "output" / "report.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("旧内部文件", encoding="utf-8")
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=task.id, dry_run=False, ok=True, message="本轮结束", response="已处理",
        turn_end_reason="completed",
    ))
    assert result.ok
    assert not (owner / "report.md").exists()
    assert legacy.read_text(encoding="utf-8") == "旧内部文件"


def test_url_and_placeholder_do_not_become_local_paths(tmp_path):
    _, task, _ = _create(tmp_path, ["https://example.com/file", "[项目目录]/file.md"])
    assert anchored_output_refs(task).anchored_refs == []
