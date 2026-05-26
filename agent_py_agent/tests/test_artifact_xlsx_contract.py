from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.contracts.staged_checkpoint_acceptance import staged_checkpoint_findings
from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture


# LLM: xlsx acceptance must validate structured workbook schema, not only zip integrity.
# 函数用途: 验证 required_columns 和 required_sheets_min 会真实参与 xlsx 机器验收。
def test_xlsx_acceptance_checks_required_columns_and_sheet_count(tmp_path: Path) -> None:
    write_xlsx_fixture(tmp_path, 
        {
            "path": "report.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"记录名": "demo", "地址": "https://example.com"}]},
                {"name": "detail", "rows": [{"记录名": "demo", "指标值": 42}]},
            ],
        }
    )
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_sheets_min": 2,
                "required_columns": ["记录名", "地址", "指标值"],
            },
        )
    )

    assert report.ok, report.to_dict()


# LLM: xlsx acceptance should reject workbook packages that lack declared schema fields.
# 函数用途: 验证缺少必需列时返回稳定 finding，避免错表通过真实 E2E。
def test_xlsx_acceptance_rejects_missing_required_columns(tmp_path: Path) -> None:
    write_xlsx_fixture(tmp_path, 
        {
            "path": "report.xlsx",
            "sheets": [{"name": "summary", "rows": [{"记录名": "demo"}]}],
        }
    )
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_sheets_min": 2,
                "required_columns": ["记录名", "地址"],
            },
        )
    )
    codes = {finding.code for finding in report.findings}

    assert not report.ok
    assert "XLSX_TOO_FEW_SHEETS" in codes
    assert "XLSX_MISSING_REQUIRED_COLUMNS" in codes


# LLM: Required spreadsheet columns are value contracts, not only header labels.
# 函数用途: 验证 xlsx 里必填列存在但数据行为空时不能通过机器验收。
def test_xlsx_acceptance_rejects_blank_required_column_values(tmp_path: Path) -> None:
    write_xlsx_fixture(tmp_path, 
        {
            "path": "report.xlsx",
            "sheets": [
                {
                    "name": "weekly",
                    "columns": ["记录名", "地址", "指标值"],
                    "rows": [{"记录名": "", "地址": "https://example.com/demo", "指标值": "42"}],
                }
            ],
        }
    )
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["记录名", "地址", "指标值"],
            },
        )
    )
    codes = {finding.code for finding in report.findings}

    assert not report.ok
    assert "XLSX_REQUIRED_COLUMN_EMPTY_VALUES" in codes


# LLM: Workbook acceptance must reject fact tables whose source JSON has no machine evidence refs.
# 函数用途: 验证 xlsx 即使生成成功，只要 staging source 缺结构化来源证据，也不能通过机器验收。
def test_xlsx_acceptance_rejects_unsourced_staged_source_json(tmp_path: Path) -> None:
    source = tmp_path / "source_data.json"
    source.write_text(
        """
{
  "sheets": [
    {
      "name": "weekly",
      "columns": ["记录名", "地址", "指标值"],
      "rows": [{"记录名": "demo", "地址": "https://example.com/demo", "指标值": 42}]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    write_xlsx_fixture(tmp_path, {"path": "report.xlsx", "source_json_path": "source_data.json"})
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["记录名", "地址", "指标值"],
                "staging_contract": {
                    "source_json_ref": "source_data.json",
                },
                "evidence_contract": {
                    "required_fields": ["指标值"],
                    "require_verified": True,
                },
            },
        )
    )

    codes = {finding.code for finding in report.findings}
    assert not report.ok
    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in codes


# LLM: Staged source JSON should reject blank required cell values before the workbook builder runs.
# 函数用途: 验证 source_data.json 的必填列空值会在阶段验收中失败，避免生成空单元格 xlsx。
def test_staged_checkpoint_rejects_blank_required_column_values(tmp_path: Path) -> None:
    source = tmp_path / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
{
  "sheets": [
    {
      "name": "汇总",
      "columns": ["记录名", "地址", "指标值"],
      "rows": [{"记录名": "", "地址": "https://example.com", "指标值": "估算"}]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )

    findings = staged_checkpoint_findings(
        [
            {
                "preferred_path": "outputs/table_report/table_report.xlsx",
                "validation_contract": {
                    "required_columns": ["记录名", "地址", "指标值"],
                    "staging_contract": {"checkpoint_refs": ["outputs/table_report/source_data.json"]},
                },
            }
        ],
        tmp_path,
    )

    assert {finding["code"] for finding in findings} == {"STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES"}


# LLM: Non-JSON staged checkpoints should enter the same generic artifact validator as final artifacts.
# 函数用途: 验证 CSV checkpoint 不能只凭存在/非空通过；结构问题必须返回统一产物验收 finding。
def test_staged_checkpoint_routes_non_json_csv_through_artifact_validator(tmp_path: Path) -> None:
    checkpoint = tmp_path / "outputs/table_report/source_data.csv"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("project,stars\n", encoding="utf-8")

    findings = staged_checkpoint_findings(
        [
            {
                "preferred_path": "outputs/table_report/table_report.xlsx",
                "validation_contract": {
                    "staging_contract": {
                        "checkpoint_refs": ["outputs/table_report/source_data.csv"],
                    },
                    "min_data_rows": 1,
                    "required_columns": ["project", "stars"],
                },
            }
        ],
        tmp_path,
    )

    assert any(finding["code"] == "CSV_INSUFFICIENT_DATA_ROWS" for finding in findings)


# LLM: staged checkpoint acceptance must use the same sheet/evidence contract as final artifact checks.
# 函数用途: 验证真实任务 acceptance 的 runtime_findings 不会丢掉 staged source 的结构和证据问题。
def test_staged_checkpoint_findings_use_validation_contract_shape_and_evidence(tmp_path: Path) -> None:
    source = tmp_path / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"sheets":[{"name":"汇总","rows":[{"记录名":"demo","地址":"https://example.com","指标值":"估算"}]}]}',
        encoding="utf-8",
    )

    findings = staged_checkpoint_findings(
        [
            {
                "preferred_path": "outputs/table_report/table_report.xlsx",
                "validation_contract": {
                    "required_sheets_min": 2,
                    "required_columns": ["记录名", "地址", "指标值"],
                    "staging_contract": {
                        "checkpoint_refs": [
                            "outputs/table_report/source_data.json",
                            "outputs/table_report/table_report.xlsx",
                        ]
                    },
                    "evidence_contract": {"required_fields": ["记录名", "地址", "指标值"], "require_verified": True},
                },
            }
        ],
        tmp_path,
    )

    codes = {finding["code"] for finding in findings}
    assert "STAGED_JSON_TOO_FEW_SHEETS" in codes
    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in codes


# LLM: Staged checkpoint paths must fail closed inside the task workspace even when refs are absolute.
# 函数用途: 验证阶段产物绝对路径越界时返回结构化 finding，不读取工作区外文件。
def test_staged_checkpoint_rejects_absolute_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-source.json"
    outside.write_text('{"rows":[{"name":"outside"}]}', encoding="utf-8")

    findings = staged_checkpoint_findings(
        [
            {
                "preferred_path": "outputs/report.xlsx",
                "validation_contract": {
                    "staging_contract": {"checkpoint_refs": [str(outside)]},
                },
            }
        ],
        tmp_path,
    )

    assert [finding["code"] for finding in findings] == ["STAGED_ARTIFACT_PATH_OUTSIDE_WORKSPACE"]


# LLM: Staging evidence should allow sourced pending claims while final delivery can still require VERIFIED.
# 函数用途: 验证阶段证据不会因为尚未标注 VERIFIED 提前卡死，除非合同显式要求 staging 也 verified。
def test_staged_checkpoint_evidence_allows_pending_claims_until_final_gate(tmp_path: Path) -> None:
    _write_pending_source(tmp_path)
    base_item = _pending_evidence_contract_item()

    findings = staged_checkpoint_findings([base_item], tmp_path)

    assert "EVIDENCE_CLAIM_UNVERIFIED" not in {finding["code"] for finding in findings}

    strict_item = {
        **base_item,
        "validation_contract": {
            **base_item["validation_contract"],
            "evidence_contract": {
                "required_fields": ["指标值"],
                "require_verified": True,
                "staging_require_verified": True,
            },
        },
    }
    strict_findings = staged_checkpoint_findings([strict_item], tmp_path)
    assert "EVIDENCE_CLAIM_UNVERIFIED" in {finding["code"] for finding in strict_findings}


def _pending_evidence_contract_item() -> dict[str, object]:
    return {
        "preferred_path": "outputs/table_report/table_report.xlsx",
        "validation_contract": {
            "staging_contract": {
                "checkpoint_refs": ["outputs/table_report/source_data.json"],
                "source_json_ref": "outputs/table_report/source_data.json",
            },
            "evidence_contract": {"required_fields": ["指标值"], "require_verified": True},
        },
    }


def _write_pending_source(root: Path) -> None:
    source = root / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
{
  "source_refs": [{"source_id": "src-1", "uri": "https://example.com/ranking"}],
  "claims": [
    {
      "claim_id": "growth-1",
      "field": "指标值",
      "value": "120",
      "source_ids": ["src-1"],
      "verification_status": "PENDING",
      "value_type": "exact"
    }
  ],
  "rows": [{"记录名": "demo", "地址": "https://example.com", "指标值": "120"}]
}
""".strip(),
        encoding="utf-8",
    )


# LLM: Metric quality must apply to staged source data before a workbook can pass.
# 函数用途: 验证 source_data.json 里的当前总量不能冒充时间窗口增量，即使字段和来源都存在。
def test_staged_checkpoint_rejects_metric_kind_mismatch(tmp_path: Path) -> None:
    _write_metric_mismatch_source(tmp_path)

    findings = staged_checkpoint_findings([_metric_mismatch_contract_item()], tmp_path)

    assert "METRIC_KIND_MISMATCH" in {finding["code"] for finding in findings}


# LLM: Staged evidence should support declared estimated values without weakening source/ref checks.
# 函数用途: 验证 source_data.json 可用 value_type/methodology 表达估算口径，并通过同一阶段证据合同。
def test_staged_checkpoint_evidence_accepts_declared_estimates(tmp_path: Path) -> None:
    _write_estimated_source(tmp_path)

    findings = staged_checkpoint_findings([_estimated_evidence_contract_item()], tmp_path)

    assert findings == []


def _write_estimated_source(root: Path) -> None:
    source = root / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
{
  "sheets": [
    {
      "name": "汇总",
      "columns": ["记录名", "地址", "指标值"],
      "rows": [{"记录名": "demo", "地址": "https://example.com", "指标值": "~100-120"}]
    }
  ],
  "source_refs": [{"source_id": "src-1", "uri": "https://example.com/ranking"}],
  "claims": [
    {
      "claim_id": "growth-1",
      "field": "指标值",
      "value": "~100-120",
      "source_ids": ["src-1"],
      "confidence": 0.7,
      "verification_status": "VERIFIED",
      "value_type": "estimated",
      "methodology": "weekly ranking overlap and current repository snapshot"
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )


def _write_metric_mismatch_source(root: Path) -> None:
    source = root / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
{
  "sheets": [
    {
      "name": "汇总",
      "rows": [{"记录名": "demo", "地址": "https://example.com", "指标值": 4991}]
    }
  ],
  "source_refs": [
    {
      "source_id": "src-current",
      "uri": "https://api.example.invalid/repos/demo",
      "reserved": {"metric_kind": "point_in_time_total"}
    }
  ],
  "claims": [
    {
      "claim_id": "claim-growth",
      "field": "指标值",
      "value": 4991,
      "source_ids": ["src-current"],
      "verification_status": "VERIFIED",
      "value_type": "exact",
      "reserved": {"metric_kind": "point_in_time_total"}
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )


def _metric_mismatch_contract_item() -> dict[str, object]:
    return {
        "preferred_path": "outputs/table_report/table_report.xlsx",
        "validation_contract": {
            "required_columns": ["记录名", "地址", "指标值"],
            "staging_contract": {"checkpoint_refs": ["outputs/table_report/source_data.json"]},
            "evidence_contract": {"required_fields": ["指标值"], "require_verified": True},
            "metric_contracts": [
                {
                    "field": "指标值",
                    "expected_kind": "time_window_delta",
                    "required_window": True,
                }
            ],
        },
    }


def _estimated_evidence_contract_item() -> dict[str, object]:
    return {
        "preferred_path": "outputs/table_report/table_report.xlsx",
        "validation_contract": {
            "required_sheets_min": 1,
            "required_columns": ["记录名", "地址", "指标值"],
            "staging_contract": {"checkpoint_refs": ["outputs/table_report/source_data.json"]},
            "evidence_contract": {
                "allowed_value_types": ["exact", "estimated"],
                "min_confidence": 0.5,
                "require_methodology_for_estimates": True,
                "require_verified": True,
                "required_fields": ["指标值"],
            },
        },
    }
