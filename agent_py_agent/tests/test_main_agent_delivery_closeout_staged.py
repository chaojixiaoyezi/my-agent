"""LLM: staged recovery tests for main-agent delivery closeout contracts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
    _should_block_on_no_progress,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    xlsx_delivery_contract,
)


def test_delivery_closeout_prefers_checkpoint_quality_actions_before_builder():
    actions = _actions_for_source('{"weekly_top10": []}')

    assert "STAGED_JSON_NO_ROWS" in actions
    assert actions["STAGED_JSON_NO_ROWS"]["recommended_action"] == "write_non_empty_structured_rows"
    assert actions["STAGED_JSON_NO_ROWS"]["checkpoint_ref"] == "outputs/table_report/source_data.json"
    assert actions["STAGED_JSON_NO_ROWS"]["writer_tool"] == "write_file"
    assert "checkpoint_shape_hint" in actions["STAGED_JSON_NO_ROWS"]
    assert actions["STAGED_JSON_NO_ROWS"]["required_columns"] == ["记录名", "地址", "指标值", "中文说明", "说明依据"]
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_treats_skeleton_only_staged_json_as_not_ready():
    actions = _actions_for_source(
        json.dumps(
            {
                "generated_date": "2026-05-20",
                "sheets": [{"week": "2026-W01", "projects": []}],
                "metadata": {"total_weeks": 20},
            },
            ensure_ascii=False,
        )
    )

    assert "STAGED_JSON_NO_ROWS" in actions
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_rejects_non_tabular_checkpoint_when_columns_required():
    actions = _actions_for_source(
        json.dumps({"weeks": [{"week_id": 1, "projects": [{"name": "demo", "url": "https://example.com/demo"}]}]}, ensure_ascii=False)
    )

    assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
    assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"] == "记录名,地址,指标值,中文说明,说明依据"
    assert "checkpoint_shape_hint" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_requires_staged_json_required_columns_before_builder():
    actions = _actions_for_source(
        json.dumps(
            {
                "sheets": [
                    {
                        "name": "week-1",
                        "columns": ["记录名", "地址", "周指标值"],
                        "rows": [{"记录名": "demo", "地址": "https://example.com", "周指标值": 10}],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
    assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert "指标值" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"]
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_requires_staged_json_evidence_before_builder():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["指标值"],
        "require_verified": True,
    }
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in actions
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["recommended_action"] == "repair_evidence_refs"
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["checkpoint_ref"] == "outputs/table_report/source_data.json"
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["required_fields"] == ["指标值"]
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["writer_tool"] == "write_file"
    assert "claims" in actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["evidence_shape_hint"]
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_missing_source_checkpoint_prefers_auditable_collection_tool():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["collection_contract"] = {
        "source_json_ref": "outputs/table_report/source_data.json",
        "required_item_evidence_fields": ["记录名", "地址", "指标值"],
        "require_completion_evidence": True,
        "require_item_evidence": True,
    }
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["记录名", "地址", "指标值"],
        "require_verified": True,
    }

    with tempfile.TemporaryDirectory() as td:
        _, actions = _enriched_report(Path(td).resolve(), contract)

    action = actions["STAGING_CHECKPOINT_MISSING"]
    assert action["checkpoint_ref"] == "outputs/table_report/source_data.json"
    assert action["checkpoint_materialization_mode"] == "source_evidence_first"
    assert action["requires_auditable_source_evidence"] is True
    assert action["writer_tool"] == "write_file"
    assert action["write_tools"] == ["write_file"]
    assert "source_refs" in action["required_structured_fields"]
    assert "claims" in action["required_structured_fields"]


def test_delivery_closeout_aggregates_missing_evidence_fields():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["required_sheets_min"] = 1
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["记录名", "地址", "指标值"],
        "require_verified": True,
    }
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    action = actions["EVIDENCE_REQUIRED_FIELD_MISSING"]
    assert set(action["required_fields"]) == {"指标值", "地址", "记录名"}
    assert action["writer_tool"] == "write_file"
    assert "source_refs" in action["evidence_shape_hint"]


def test_delivery_closeout_requires_staged_json_min_sheet_count_before_builder():
    contract = xlsx_delivery_contract()
    contract["artifacts"][0]["validation_contract"].pop("evidence_contract", None)
    contract["artifacts"][0]["validation_contract"]["required_sheets_min"] = 2
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "STAGED_JSON_TOO_FEW_SHEETS" in actions
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["required_sheets_min"] == 2
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["writer_tool"] == "write_file"
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_adds_generic_staging_builder_action_for_ready_source():
    contract = xlsx_delivery_contract()
    contract["artifacts"][0]["validation_contract"]["required_sheets_min"] = 1
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "STAGING_BUILDER_READY" in actions
    assert actions["STAGING_BUILDER_READY"]["recommended_action"] == "write_target_artifact"
    assert actions["STAGING_BUILDER_READY"]["builder_tool"] == "write_file"
    assert actions["STAGING_BUILDER_READY"]["source_ref"] == "outputs/table_report/source_data.json"


def test_delivery_closeout_warns_for_failed_existing_workbook_content_shape():
    from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        _write_source(workspace, _valid_rows_json())
        write_xlsx_fixture(
            workspace,
            {
                "path": "outputs/table_report/table_report.xlsx",
                "sheets": [
                    {
                        "name": "week-1",
                        "columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
                        "rows": [
                            {
                                "记录名": "",
                                "地址": "https://example.com",
                                "指标值": 10,
                                "中文说明": "demo",
                                "说明依据": "demo",
                            }
                        ],
                    }
                ],
            }
        )
        report, actions = _enriched_report(workspace, contract)

        assert report["ok"] is True
        assert "STAGING_BUILDER_READY" not in actions
        findings = report["artifacts"][0]["acceptance_report"]["findings"]
        assert "XLSX_REQUIRED_COLUMN_EMPTY_VALUES" in {finding["code"] for finding in findings}
        assert {finding["severity"] for finding in findings} == {"warning"}


def test_delivery_closeout_reports_invalid_checkpoint_json():
    actions = _actions_for_source('[{"记录名":"demo","地址":"https://example.com"')

    assert "STAGED_JSON_INVALID" in actions
    assert actions["STAGED_JSON_INVALID"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert actions["STAGED_JSON_INVALID"]["checkpoint_ref"] == "outputs/table_report/source_data.json"
    assert "parse_error" in actions["STAGED_JSON_INVALID"]
    assert actions["STAGED_JSON_INVALID"]["writer_tool"] == "write_file"
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_does_not_materialize_builder_output_before_source_is_ready():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        report, _ = _enriched_report_for_source(workspace, '[{"记录名":"demo","地址":"https://example.com"', contract)
        actions = report["delivery_progress"]["recovery_actions"]

        builder_output_refs = {
            item["validation_contract"]["staging_contract"]["workbook_ref"]
            for item in contract["artifacts"]
        }
        assert not [
            action
            for action in actions
            if action.get("recommended_action") == "materialize_checkpoint"
            and action.get("checkpoint_ref") in builder_output_refs
        ]


def test_delivery_closeout_repeated_staged_failure_remains_repairable_by_default():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        report, actions = _enriched_report_for_source(workspace, '[{"记录名":"demo","地址":"https://example.com"', contract)
        previous = _previous_delivery_progress(report, unchanged_failure_count=5)

        from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
            DeliveryProgressContext,
            _enrich_delivery_progress,
            _should_block_on_no_progress,
        )

        enriched = _enrich_delivery_progress(report, previous, DeliveryProgressContext(workspace, contract))

        assert "STAGED_JSON_INVALID" in actions
        assert enriched["delivery_progress"]["unchanged_failure_count"] >= 6
        assert _should_block_on_no_progress(enriched, contract=contract, workspace_root=workspace) is False


def test_delivery_closeout_zero_no_progress_threshold_is_unlimited():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()

        report = {
            "ok": False,
            "artifacts": [],
            "delivery_progress": {
                "unchanged_failure_count": 999,
                "no_progress_block_threshold": 0,
                "recovery_actions": [],
            },
        }

        assert _should_block_on_no_progress(report, contract={}, workspace_root=workspace) is False


def test_delivery_progress_tracks_contract_declared_artifact_roots():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _custom_root_artifact_contract()
        report = _failed_custom_root_artifact_report(workspace)

        from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
            DeliveryProgressContext,
            _enrich_delivery_progress,
        )

        first = _enrich_delivery_progress(report, {}, DeliveryProgressContext(workspace, contract))
        target = workspace / "lab_outputs/custom-artifact/index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<!doctype html><html><body><input id='email'></body></html>", encoding="utf-8")
        second = _enrich_delivery_progress(report, first, DeliveryProgressContext(workspace, contract))

        assert second["delivery_progress"]["unchanged_failure_count"] == 1


def test_delivery_closeout_keeps_larger_static_site_finding_batches():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = {
            "artifacts": [
                {
                    "artifact_id": "site",
                    "kind": "web_project",
                    "path": str(workspace / "outputs/site"),
                    "validation_contract": {
                        "validator": "static_site_check",
                        "required_files": ["index.html", "app.js"],
                    },
                }
            ]
        }
        site = workspace / "outputs/site"
        site.mkdir(parents=True)
        (site / "index.html").write_text(
            '<!doctype html><html><body><script src="app.js"></script></body></html>',
            encoding="utf-8",
        )
        js_refs = "\n".join(f"document.getElementById('field{i}');" for i in range(24))
        (site / "app.js").write_text(js_refs, encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert "getElementById:field0" in action["finding_values"]
        assert "getElementById:field23" in action["finding_values"]
        assert len(action["finding_values"]) >= 24


def _actions_for_source(source_content: str, *, contract: dict[str, object] | None = None) -> dict[str, dict[str, object]]:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        _, actions = _enriched_report_for_source(workspace, source_content, contract or xlsx_delivery_contract())
        return actions


def _enriched_report_for_source(
    workspace: Path,
    source_content: str,
    contract: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
        DeliveryContractValidationRequest,
        DeliveryProgressContext,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    _write_source(workspace, source_content)
    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=workspace,
            params=RunParams(delivery_contract=contract, save=False),
        )
    )
    enriched = _enrich_delivery_progress(report, {}, DeliveryProgressContext(workspace, contract))
    actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}
    return enriched, actions


def _enriched_report(
    workspace: Path,
    contract: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
        DeliveryContractValidationRequest,
        DeliveryProgressContext,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=workspace,
            params=RunParams(delivery_contract=contract, save=False),
        )
    )
    enriched = _enrich_delivery_progress(report, {}, DeliveryProgressContext(workspace, contract))
    actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}
    return enriched, actions


def _custom_root_artifact_contract() -> dict[str, object]:
    return {
        "case_id": "custom_root_artifact_case",
        "bootstrap_contract": {
            "materialization_targets": [
                {"workspace_relative_path": f"lab_outputs/custom-artifact/{name}"}
                for name in ("index.html", "styles.css", "app.js", "README.md")
            ]
        },
        "artifacts": [
            {
                "artifact_id": "custom_artifact_root",
                "kind": "web_project",
                "preferred_path": "lab_outputs/custom-artifact",
                "required": True,
                "validation_contract": {"validator": "static_site_check"},
            }
        ],
    }


def _failed_custom_root_artifact_report(workspace: Path) -> dict[str, object]:
    return {
        "case_id": "custom_root_artifact_case",
        "ok": False,
        "artifacts": [
            {
                "artifact_id": "custom_artifact_root",
                "kind": "web_project",
                "path": str(workspace / "lab_outputs/custom-artifact"),
                "ok": False,
                "acceptance_report": {
                    "findings": [
                        {
                            "code": "STATIC_SITE_MISSING_DOM_ID_HITS",
                            "location": "missing_dom_id_hits",
                            "value": "getElementById:email",
                        }
                    ],
                    "ok": False,
                },
            }
        ],
    }


def _write_source(workspace: Path, source_content: str) -> None:
    source = workspace / "outputs/table_report/source_data.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(source_content, encoding="utf-8")


def _previous_delivery_progress(report: dict[str, object], *, unchanged_failure_count: int) -> dict[str, object]:
    progress = report["delivery_progress"]
    return {
        "ok": False,
        "delivery_progress": {
            "failure_fingerprint": progress["failure_fingerprint"],
            "work_progress_fingerprint": progress["work_progress_fingerprint"],
            "unchanged_failure_count": unchanged_failure_count,
        },
    }


def _valid_rows_json() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
                    "rows": [
                        {
                            "记录名": "demo",
                            "地址": "https://example.com",
                            "指标值": 10,
                            "中文说明": "demo",
                            "说明依据": "demo",
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
