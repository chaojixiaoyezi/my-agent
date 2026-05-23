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


def test_delivery_closeout_uses_artifact_locator_when_path_is_not_declared(tmp_path):
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifacts import (
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
