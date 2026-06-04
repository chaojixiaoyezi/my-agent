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
    register_artifact(
        ArtifactRegistration(
            workspace_root=tmp_path,
            path=artifact,
            artifact_id="report",
            run_id="run-1",
        )
    )
    with registry_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("{bad-json\n")

    report = latest_artifact_records_report(tmp_path)

    assert report.records["report"].path == str(artifact.resolve())
    assert report.errors[0]["context"] == "artifact_registry.read_line"
    assert report.errors[0]["category"] == "data_parse"


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
    html.write_text("<!doctype html><html><head></head><body>ok</body></html>", encoding="utf-8")
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


def test_closeout_prefers_registry_record_for_artifact_id_over_stale_contract_path(tmp_path: Path):
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
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
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
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


def test_closeout_surfaces_registry_parse_error_in_artifact_report(tmp_path: Path):
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )
    from agent_py_agent.agent.artifacts.registry import registry_path

    registry_path(tmp_path).parent.mkdir(parents=True)
    registry_path(tmp_path).write_text("{bad-json\n", encoding="utf-8")
    contract = {
        "artifacts": [
            {
                "artifact_id": "missing_report",
                "kind": "md",
                "preferred_path": "outputs/missing.md",
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

    assert report["ok"] is False
    assert report["registry_read_errors"][0]["context"] == "artifact_registry.read_line"
    acceptance = report["artifacts"][0]["acceptance_report"]
    assert acceptance["registry_read_errors"][0]["category"] == "data_parse"


def test_closeout_ignores_legacy_manifest_and_requires_canonical_registry_for_group(tmp_path: Path):
    _write_site_files(tmp_path)
    _write_legacy_group_manifest(tmp_path)

    report = _validate_group_contract(tmp_path)

    artifact = report["artifacts"][0]
    assert report["ok"] is False
    assert artifact["artifact_id"] == "shopping_site_ui"
    finding = artifact["acceptance_report"]["findings"][0]
    assert finding["code"] in {"ARTIFACT_LOCATOR_NO_MATCH", "ARTIFACT_LOCATOR_AMBIGUOUS"}
    assert "data/artifacts/registry.jsonl" in finding["registry_ref"]


def test_closeout_accepts_registry_declared_file_group(tmp_path: Path):
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records

    site = _write_site_files(tmp_path)
    _register_site_group(tmp_path, site)
    report = _validate_group_contract(tmp_path)
    artifact = report["artifacts"][0]
    records = latest_artifact_records(tmp_path)
    assert report["ok"] is True
    assert artifact["artifact_id"] == "shopping_site_ui"
    assert artifact["kind"] == "group"
    assert artifact["paths"] == [
        str((site / "index.html").resolve()),
        str((site / "styles.css").resolve()),
        str((site / "app.js").resolve()),
    ]
    assert records["shopping_site_ui"].metadata["artifact_type"] == "file_group"
    assert len(records["shopping_site_ui"].metadata["members"]) == 3


def _write_site_files(tmp_path: Path) -> Path:
    site = tmp_path / "outputs" / "shopping_site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><head><script src='app.js'></script></head><body>ok</body></html>",
        encoding="utf-8",
    )
    (site / "styles.css").write_text("body { color: #111; }", encoding="utf-8")
    (site / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return site


def _file_group_contract() -> dict:
    return {
        "artifacts": [
            {
                "artifact_id": "shopping_site_ui",
                "artifact_intent": {"acceptable_extensions": [".html", ".css", ".js"]},
                "allowed_output_roots": ["outputs"],
            }
        ]
    }


def _validate_group_contract(tmp_path: Path) -> dict:
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )

    contract = _file_group_contract()
    return _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=tmp_path,
            params=_empty_tool_loop_params(),
        )
    )


def _write_legacy_group_manifest(tmp_path: Path) -> None:
    manifest_dir = tmp_path / ".agent_delivery"
    manifest_dir.mkdir()
    (manifest_dir / "artifacts_manifest.json").write_text(_legacy_group_manifest_json(), encoding="utf-8")


def _legacy_group_manifest_json() -> str:
    return """
    {
      "schema_version": "delivery_contract.v1",
      "artifacts": [
        {
          "artifact_id": "shopping_site_ui",
          "required": true,
          "files": [
            {"path": "outputs/shopping_site/index.html", "kind": "html"},
            {"path": "outputs/shopping_site/styles.css", "kind": "css"},
            {"path": "outputs/shopping_site/app.js", "kind": "js"}
          ]
        }
      ]
    }
    """


def _register_site_group(tmp_path: Path, site: Path) -> None:
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactGroupRegistration,
        register_artifact_group,
    )

    register_artifact_group(
        ArtifactGroupRegistration(
            workspace_root=tmp_path,
            paths=[site / "index.html", site / "styles.css", site / "app.js"],
            artifact_id="shopping_site_ui",
            run_id="run-1",
            source="test",
        )
    )


def test_subagent_file_path_alias_is_recovered_as_artifact_item():
    from agent_py_agent.agent.subagents.parsing.artifacts import artifact_items_from_payload

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
    from agent_py_agent.agent.tooling.filesystem import WriteFileTool

    result = WriteFileTool(tmp_path).execute({"path": "outputs/report.md", "content": "hello"})

    assert result.ok is True
    assert result.result_envelope["path"] == str((tmp_path / "outputs" / "report.md").resolve())
    assert result.result_envelope["artifact_ref"] == result.result_envelope["path"]


def test_tool_archive_registers_write_file_artifact(tmp_path: Path):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
    from agent_py_agent.agent.artifacts.registry import latest_artifact_records
    from agent_py_agent.agent.tooling.filesystem import WriteFileTool

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
