from __future__ import annotations


def test_artifact_locator_finds_single_required_kind_under_allowed_root(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    workbook = tmp_path / "outputs" / "weekly.xlsx"
    workbook.write_bytes(b"PK\x03\x04fake")

    result = locate_artifact(
        {"artifact_id": "weekly_workbook", "kind": "xlsx", "allowed_output_roots": ["outputs"]},
        tmp_path,
    )

    assert result.path == workbook.resolve()
    assert result.findings == []


def test_artifact_locator_accepts_user_requested_absolute_output_root(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    requested = tmp_path.parent / "requested-output"
    requested.mkdir()
    output = requested / "final_report.md"
    output.write_text("done", encoding="utf-8")

    result = locate_artifact(
        {"artifact_id": "final_report", "kind": "md", "allowed_output_roots": [str(requested)]},
        tmp_path,
    )

    assert result.path == output.resolve()
    assert result.findings == []


def test_artifact_locator_treats_spreadsheet_as_open_workbook_family(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    workbook = tmp_path / "outputs" / "weekly_top20_stars_2026.xlsx"
    workbook.write_bytes(b"PK\x03\x04fake")

    result = locate_artifact(
        {"artifact_id": "github_weekly_stars_xlsx", "kind": "spreadsheet", "allowed_output_roots": ["outputs"]},
        tmp_path,
    )

    assert result.path == workbook.resolve()
    assert result.findings == []


def test_artifact_locator_refuses_ambiguous_kind_without_structured_hint(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "a.xlsx").write_bytes(b"PK\x03\x04a")
    (tmp_path / "outputs" / "b.xlsx").write_bytes(b"PK\x03\x04b")

    result = locate_artifact({"artifact_id": "workbook", "kind": "xlsx", "allowed_output_roots": ["outputs"]}, tmp_path)

    assert result.path is None
    assert result.findings[0]["code"] == "ARTIFACT_LOCATOR_AMBIGUOUS"


def test_artifact_locator_derives_unknown_kind_extension(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    artifact = tmp_path / "outputs" / "dataset.parquet"
    artifact.write_bytes(b"PAR1")

    result = locate_artifact(
        {"artifact_id": "dataset", "kind": "parquet", "allowed_output_roots": ["outputs"]},
        tmp_path,
    )

    assert result.path == artifact.resolve()
    assert result.findings == []


def test_artifact_locator_accepts_explicit_extension_override(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    artifact = tmp_path / "outputs" / "payload.custombin"
    artifact.write_bytes(b"custom")

    result = locate_artifact(
        {
            "artifact_id": "payload",
            "kind": "opaque-binary",
            "file_extensions": ["custombin"],
            "allowed_output_roots": ["outputs"],
        },
        tmp_path,
    )

    assert result.path == artifact.resolve()
    assert result.findings == []


def test_artifact_locator_uses_materialized_acceptable_extensions_for_broad_kind_label(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import (
        artifact_can_be_located,
        locate_artifact,
    )

    (tmp_path / "outputs").mkdir()
    artifact = tmp_path / "outputs" / "bearing_mount.dxf"
    artifact.write_text("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    contract = {
        "artifact_id": "cad_drawing",
        "kind_label": "CAD 图纸",
        "acceptable_extensions": ["dxf", "step", "stp"],
        "preferred_extension": "dxf",
        "allowed_output_roots": ["outputs"],
    }

    assert artifact_can_be_located(contract) is True
    result = locate_artifact(contract, tmp_path)

    assert result.path == artifact.resolve()
    assert result.findings == []


def test_artifact_locator_uses_nested_artifact_intent_extensions(tmp_path):
    from agent_py_agent.agent.agent_core.artifact_locator import locate_artifact

    (tmp_path / "outputs").mkdir()
    artifact = tmp_path / "outputs" / "assembly.step"
    artifact.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")

    result = locate_artifact(
        {
            "artifact_id": "cad_model",
            "kind": "cad",
            "artifact_intent": {
                "kind_label": "CAD 三维模型",
                "acceptable_extensions": ["step", "stp", "iges"],
                "preferred_extension": "step",
            },
            "allowed_output_roots": ["outputs"],
        },
        tmp_path,
    )

    assert result.path == artifact.resolve()
    assert result.findings == []


def test_delivery_closeout_uses_artifact_locator_when_path_is_not_declared(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )

    (tmp_path / "outputs").mkdir()
    artifact_path = tmp_path / "outputs" / "final.txt"
    artifact_path.write_text("enough content for a text artifact", encoding="utf-8")
    contract = {
        "artifacts": [
            {
                "artifact_id": "final_text",
                "kind": "txt",
                "allowed_output_roots": ["outputs"],
                "validation_contract": {"min_size": 10},
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

    assert report["ok"] is True
    assert report["artifacts"][0]["path"] == str(artifact_path.resolve())


def test_delivery_closeout_locates_spreadsheet_artifact_without_declared_path(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )

    (tmp_path / "outputs").mkdir()
    artifact_path = tmp_path / "outputs" / "weekly_top20_stars_2026.xlsx"
    artifact_path.write_bytes(b"PK\x03\x04fake")
    contract = {
        "artifacts": [
            {
                "artifact_id": "github_weekly_stars_xlsx",
                "kind": "spreadsheet",
                "allowed_output_roots": ["outputs"],
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

    assert report["artifacts"][0]["path"] == str(artifact_path.resolve())
    assert report["artifacts"][0]["acceptance_report"]["artifact_ref"] == str(artifact_path.resolve())


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
