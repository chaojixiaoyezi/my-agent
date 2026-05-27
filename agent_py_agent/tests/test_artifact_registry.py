from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


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


def test_closeout_prefers_registry_record_for_artifact_id_over_stale_contract_path(tmp_path: Path):
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    workbook = tmp_path / "weekly_top20_stars_2026.xlsx"
    workbook.write_bytes(b"PK\x03\x04fake")
    register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=workbook,
            artifact_id="github_weekly_stars_xlsx",
            run_id="run-1",
            task_id="task-1",
            agent_id="agent-1",
            kind="spreadsheet",
            source="test",
        )
    )
    contract = {
        "artifacts": [
            {
                "artifact_id": "github_weekly_stars_xlsx",
                "kind": "spreadsheet",
                "preferred_path": "outputs/github_stars/weekly_top20_stars_2026.xlsx",
            }
        ]
    }

    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=tmp_path,
            params=_empty_tool_loop_params(),
        )
    )

    assert report["artifacts"][0]["path"] == str(workbook.resolve())
    assert report["artifacts"][0]["registry_ref"]["artifact_id"] == "github_weekly_stars_xlsx"


def test_closeout_does_not_mark_invalid_artifact_ready(tmp_path: Path):
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records

    artifact = tmp_path / "outputs" / "broken.xlsx"
    artifact.parent.mkdir()
    artifact.write_bytes(b"not a workbook")
    contract = {
        "artifacts": [
            {
                "artifact_id": "final_workbook",
                "kind": "xlsx",
                "preferred_path": "outputs/broken.xlsx",
            }
        ]
    }

    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=tmp_path,
            params=_empty_tool_loop_params(),
        )
    )

    record = latest_artifact_records(tmp_path)["final_workbook"]
    assert report["ok"] is False
    assert record.status == "invalid"


def test_subagent_file_path_alias_is_recovered_as_artifact_item():
    from agent_py_agent.agent.subagents.parsing_artifacts import artifact_items_from_payload

    items = artifact_items_from_payload(
        {
            "summary": "表格已生成",
            "file_path": "/tmp/run/weekly_top20_stars_2026.xlsx",
        }
    )

    assert items == [
        {
            "path": "/tmp/run/weekly_top20_stars_2026.xlsx",
            "kind": "file",
            "summary": "reported file_path artifact",
        }
    ]


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
    mock_task.output_dir = str(task_dir)
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.attributes = {}

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
    from agent_py_agent.agent.tools import WriteFileTool

    result = WriteFileTool(tmp_path).execute({"path": "outputs/report.md", "content": "hello"})

    assert result.ok is True
    assert result.result_envelope["path"] == str((tmp_path / "outputs" / "report.md").resolve())
    assert result.result_envelope["artifact_ref"] == result.result_envelope["path"]


def test_tool_archive_registers_write_file_artifact(tmp_path: Path):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_round_execution import ToolCallRecordParams
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tools import WriteFileTool

    result = WriteFileTool(tmp_path).execute({"path": "outputs/report.md", "content": "hello"})
    params = _empty_tool_loop_params()
    archive_tool_call_record(
        SimpleNamespace(root=tmp_path, config=SimpleNamespace()),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "outputs/report.md"},
            result=result,
        ),
    )

    records = latest_artifact_records(tmp_path)
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record.path == str((tmp_path / "outputs" / "report.md").resolve())
    assert record.created_by_tool == "write_file"


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
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
