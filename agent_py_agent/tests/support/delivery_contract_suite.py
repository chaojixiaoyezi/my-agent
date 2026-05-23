from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


# LLM: run_delivery_contract_suite is the reusable contract-suite pattern for delivery contracts.
# 函数用途: 复用一组通用合同校验用例，避免每个入口各写一套散测试。
def run_delivery_contract_suite(validate: Callable[..., object], workspace: Path) -> None:
    _assert_invalid_artifacts_shape(validate, workspace)
    _assert_valid_open_world_artifact(validate, workspace)
    _assert_outside_workspace_path_rejected(validate, workspace)
    _assert_version_mismatch_is_observable(validate, workspace)


def _report_dict(report: object) -> dict[str, object]:
    if hasattr(report, "to_dict"):
        value = report.to_dict()
        assert isinstance(value, dict)
        return value
    assert isinstance(report, dict)
    return report


def _codes(report: object) -> set[str]:
    payload = _report_dict(report)
    findings = payload.get("findings")
    assert isinstance(findings, list)
    return {str(item.get("code")) for item in findings if isinstance(item, dict)}


def _assert_invalid_artifacts_shape(validate: Callable[..., object], workspace: Path) -> None:
    report = validate({"schema_version": "delivery_contract.v1", "artifacts": "bad"}, workspace_root=workspace)

    assert _report_dict(report)["ok"] is False
    assert "DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST" in _codes(report)


def _assert_valid_open_world_artifact(validate: Callable[..., object], workspace: Path) -> None:
    report = validate(
        {
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "dataset",
                    "kind": "parquet",
                    "allowed_output_roots": ["outputs"],
                    "validation_contract": {"file_extensions": ["parquet"]},
                }
            ],
        },
        workspace_root=workspace,
    )

    assert _report_dict(report)["ok"] is True


def _assert_outside_workspace_path_rejected(validate: Callable[..., object], workspace: Path) -> None:
    outside = workspace.parent / "outside.xlsx"
    report = validate(
        {
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "bad", "kind": "xlsx", "path": str(outside)}],
        },
        workspace_root=workspace,
    )

    assert _report_dict(report)["ok"] is False
    assert "DELIVERY_CONTRACT_ARTIFACT_PATH_OUTSIDE_WORKSPACE" in _codes(report)


def _assert_version_mismatch_is_observable(validate: Callable[..., object], workspace: Path) -> None:
    report = validate(
        {
            "schema_version": "delivery_contract.v0",
            "artifacts": [{"artifact_id": "report", "kind": "md", "allowed_output_roots": ["outputs"]}],
        },
        workspace_root=workspace,
    )

    assert _report_dict(report)["ok"] is True
    assert "DELIVERY_CONTRACT_SCHEMA_VERSION_MISMATCH" in _codes(report)
