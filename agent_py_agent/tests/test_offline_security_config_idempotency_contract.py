from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.contracts.idempotency import decide_idempotency, idempotency_key
from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config


def test_validate_artifact_rejects_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Summary\n# Evidence\n", encoding="utf-8")

    result = validate_artifact(ArtifactAcceptanceRequest(path=outside, workspace_root=workspace))

    assert result.ok is False
    assert [item.code for item in result.findings] == ["ARTIFACT_PATH_OUTSIDE_WORKSPACE"]


def test_runtime_config_rejects_artifact_dir_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside-artifacts"

    result = validate_runtime_config(
        {
            "workspace_root": str(workspace),
            "artifact_dir": str(outside),
            "tool_timeout": 30,
            "max_steps": 10,
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CONFIG_ARTIFACT_DIR_OUTSIDE_WORKSPACE",)


def test_runtime_config_ignores_allowed_write_roots_as_unknown_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = validate_runtime_config(
        {
            "workspace_root": str(workspace),
            "artifact_dir": "artifacts",
            "tool_timeout": 30,
            "max_steps": 10,
            "allowed_write_roots": ["/"],
        }
    )

    assert result.ok is False
    assert "CONFIG_UNKNOWN_FIELD" in result.error_codes
    assert "CONFIG_ALLOWED_WRITE_ROOT_DANGEROUS" not in result.error_codes


def test_idempotency_key_is_stable_and_reuses_existing_ids() -> None:
    left = {"target": "artifact://report", "args": {"b": 2, "a": 1}}
    right = {"args": {"a": 1, "b": 2}, "target": "artifact://report"}

    assert idempotency_key("artifact_write", left) == idempotency_key("artifact_write", right)
    decision = decide_idempotency("artifact_write", right, existing_ids=["write-1"])

    assert decision.action == "reuse_existing"
    assert decision.existing_ids == ["write-1"]


def test_contract_mutation_removing_required_markdown_section_fails(tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    report_path.write_text("# Summary\nok\n# Evidence\nsource\n", encoding="utf-8")
    contract = {"required_sections": ["Summary", "Evidence"]}

    assert validate_artifact(ArtifactAcceptanceRequest(path=report_path, workspace_root=tmp_path, validation_contract=contract)).ok

    report_path.write_text("# Summary\nok\n", encoding="utf-8")
    result = validate_artifact(ArtifactAcceptanceRequest(path=report_path, workspace_root=tmp_path, validation_contract=contract))

    assert result.ok is False
    assert [item.code for item in result.findings] == ["MARKDOWN_REQUIRED_SECTION_MISSING"]
