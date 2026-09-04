from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_artifact_registry_updates_same_artifact_id_path(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records,
        register_artifact,
    )

    first = tmp_path / "outputs" / "weekly.xlsx"
    first.parent.mkdir()
    first.write_bytes(b"first")
    second = tmp_path / "weekly.xlsx"
    second.write_bytes(b"second")
    created = register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=first,
            artifact_id="github_weekly",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="xlsx",
            source="test",
        )
    )
    updated = register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=second,
            artifact_id=created.artifact_id,
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="xlsx",
            source="test",
        )
    )

    assert updated.artifact_id == "github_weekly"
    assert updated.path == str(second.resolve())
    assert updated.sha256 != created.sha256
    assert latest_artifact_records(tmp_path)["github_weekly"].path == str(second.resolve())


def test_artifact_registry_reports_bad_rows_without_losing_good_records(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records_report,
        register_artifact,
        registry_path,
    )

    artifact = tmp_path / "outputs" / "report.md"
    artifact.parent.mkdir()
    artifact.write_text("ok", encoding="utf-8")
    register_artifact(ArtifactRegistration(workspace_root=tmp_path, path=artifact, artifact_id="report", run_id="run-1"))
    with registry_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("{bad-json\n")

    report = latest_artifact_records_report(tmp_path)

    assert report.records["report"].path == str(artifact.resolve())
    assert report.errors[0]["context"] == "artifact_registry.read_line"
    assert report.errors[0]["category"] == "data_parse"


def test_artifact_registry_rejects_replaced_workspace_root_symlink(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        latest_artifact_records_report,
        register_observed_artifact,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = tmp_path / "workspace-original"
    workspace.rename(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    report = latest_artifact_records_report(workspace)

    assert report.records == {}
    assert report.errors
    with pytest.raises(OSError):
        register_observed_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=workspace / "result.txt",
                artifact_id="result",
                status="ready",
            ),
            observed_sha256="digest",
            observed_size_bytes=1,
        )
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (outside / "data" / "artifacts" / "registry.jsonl").exists()


def test_artifact_registry_rejects_hardlinked_ledger(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
        registry_path,
    )

    workspace = tmp_path / "workspace"
    ledger = registry_path(workspace)
    ledger.parent.mkdir(parents=True)
    outside = tmp_path / "outside-ledger.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    try:
        ledger.hardlink_to(outside)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this platform")

    with pytest.raises(OSError):
        register_observed_artifact(
            ArtifactRegistration(
                workspace_root=workspace,
                path=workspace / "result.txt",
                artifact_id="result",
                status="ready",
            ),
            observed_sha256="digest",
            observed_size_bytes=1,
        )

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_artifact_registry_records_logical_file_group(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactGroupRegistration,
        latest_artifact_records,
        register_artifact_group,
    )

    site = tmp_path / "outputs" / "site"
    site.mkdir(parents=True)
    html = site / "index.html"
    css = site / "styles.css"
    html.write_text("<!doctype html><html><body>ok</body></html>", encoding="utf-8")
    css.write_text("body { color: black; }", encoding="utf-8")
    registered = register_artifact_group(
        ArtifactGroupRegistration(
            workspace_root=tmp_path,
            paths=[html, css],
            artifact_id="site_bundle",
            run_id="run-1",
            source="test",
        )
    )

    record = latest_artifact_records(tmp_path)["site_bundle"]
    assert registered.status == "ready"
    assert record.path == str(site.resolve())
    assert record.metadata["artifact_type"] == "file_group"
    assert [row["relative_path"] for row in record.metadata["members"]] == [
        "outputs/site/index.html",
        "outputs/site/styles.css",
    ]


def test_subagent_file_path_field_is_not_recovered_as_artifact_item():
    from agent_py_agent.agent.subagents.parsing.artifacts import artifact_items_from_payload

    assert artifact_items_from_payload({"summary": "表格已生成", "file_path": "/tmp/run/weekly.xlsx"}) == []


def test_subagent_structured_artifact_registers_registry_ref(mock_task, tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
    from agent_py_agent.agent.subagents.result_processors import _process_structured_output

    task_dir = tmp_path / "data" / "subagents" / "run-1"
    task_dir.mkdir(parents=True)
    artifact = task_dir / "weekly.xlsx"
    artifact.write_bytes(b"workbook")
    mock_task.id = "run-1"
    mock_task.root_id = "root-1"
    mock_task.task_dir = str(task_dir)
    mock_task.task_workspace_dir = str(tmp_path)
    mock_task.output_dir = str(task_dir)
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.attributes = {"workspace_root": str(tmp_path)}

    result = _process_structured_output(
        mock_task,
        SubAgentParsedOutput(
            found=True,
            ok=True,
            parse_error="",
            status="DONE",
            artifacts=[{"path": "weekly.xlsx", "kind": "xlsx", "summary": "周报"}],
        ),
        123456.0,
        None,
    )

    assert result["artifacts"][0]["registry_ref"]["path"] == str(artifact)
    assert mock_task.attributes["artifact_registry_refs"][0]["path"] == str(artifact)
    assert latest_artifact_records(tmp_path)[result["artifacts"][0]["artifact_id"]].path == str(artifact)


def test_write_file_result_exposes_machine_path_for_registry(tmp_path: Path):
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool

    result = WriteFileTool(tmp_path).execute({"path": "outputs/report.md", "content": "hello"})

    assert result.ok is True
    assert result.result_envelope["path"] == str((tmp_path / "outputs" / "report.md").resolve())
    assert result.result_envelope["artifact_ref"] == result.result_envelope["path"]


def test_tool_archive_registers_write_file_artifact(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    outcome = WriteFileTool(tmp_path).execute(
        {"path": "outputs/report.md", "content": "hello"}
    )
    call = canonical_history_call(
        "write_file",
        {"path": "outputs/report.md"},
        call_id="write-report",
        run_id="run-1",
    )
    result = canonical_history_result(
        call,
        outcome.output,
        handler_details=outcome.result_envelope,
    )
    params = _empty_tool_loop_params()
    archive_tool_call_record(
        SimpleNamespace(root=tmp_path, config=SimpleNamespace()),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=call,
            result=result,
        ),
    )

    records = latest_artifact_records(tmp_path)
    assert len(records) == 1
    assert next(iter(records.values())).path == str((tmp_path / "outputs" / "report.md").resolve())


def _empty_tool_loop_params():
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
