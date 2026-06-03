from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.contracts.artifact_collection_contract import collection_contract_findings
from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture


def test_collection_contract_rejects_too_few_structured_groups(tmp_path: Path) -> None:
    source = tmp_path / "source_data.json"
    source.write_text(
        json.dumps(
            {
                "sheets": [
                    _sheet("week-1", 10),
                    _sheet("week-2", 10),
                    _sheet("week-3", 10),
                ],
                "source_refs": [_source_ref("src-1")],
                "claims": [_claim("记录名", "src-1")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_xlsx_fixture(tmp_path, {"path": "report.xlsx", "source_json_path": "source_data.json"})

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["记录名", "地址", "指标值"],
                "staging_contract": {"source_json_ref": "source_data.json"},
                "collection_contract": {
                    "source_json_ref": "source_data.json",
                    "groups_path": "sheets",
                    "items_path": "rows",
                    "min_groups": 20,
                    "min_items_per_group": 10,
                    "required_item_fields": ["记录名", "地址", "指标值"],
                },
            },
        )
    )

    assert report.ok is True
    assert "COLLECTION_TOO_FEW_GROUPS" in {finding.code for finding in report.findings}


def test_collection_contract_rejects_group_with_too_few_items(tmp_path: Path) -> None:
    source = tmp_path / "source_data.json"
    source.write_text(
        json.dumps(
            {
                "sheets": [_sheet("week-1", 10), _sheet("week-2", 2)],
                "source_refs": [_source_ref("src-1")],
                "claims": [_claim("记录名", "src-1")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_xlsx_fixture(tmp_path, {"path": "report.xlsx", "source_json_path": "source_data.json"})

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["记录名", "地址", "指标值"],
                "staging_contract": {"source_json_ref": "source_data.json"},
                "collection_contract": {
                    "source_json_ref": "source_data.json",
                    "groups_path": "sheets",
                    "items_path": "rows",
                    "min_groups": 2,
                    "min_items_per_group": 10,
                    "required_item_fields": ["记录名", "地址", "指标值"],
                },
            },
        )
    )

    assert report.ok is True
    assert "COLLECTION_GROUP_TOO_FEW_ITEMS" in {finding.code for finding in report.findings}


def test_collection_contract_rejects_incomplete_source_index_and_mapping(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs/docs"
    outputs.mkdir(parents=True)
    (outputs / "report.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")
    (outputs / "report.md").write_text("# 翻译稿\n\n只包含 Paper 1\n", encoding="utf-8")
    (outputs / "source_index.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"title": "Paper 1", "url": "https://example.com/1", "date": "2026-01-01"},
                    {"title": "Paper 2", "url": "https://example.com/2", "date": "2026-02-01"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=outputs / "report.pdf",
            workspace_root=tmp_path,
            validation_contract={
                "validator": "document_acceptance",
                "collection_contract": {
                    "source_json_ref": "outputs/docs/source_index.json",
                    "items_path": "rows",
                    "min_items_total": 5,
                    "required_item_fields": ["title", "url", "date"],
                    "require_completion_evidence": True,
                    "mapping": {
                        "artifact_ref": "outputs/docs/report.md",
                        "key_fields": ["title"],
                        "min_mapped_items": 5,
                    },
                },
            },
        )
    )

    codes = {finding.code for finding in report.findings}
    assert report.ok is True
    assert "COLLECTION_TOO_FEW_ITEMS" in codes
    assert "COLLECTION_COMPLETENESS_EVIDENCE_MISSING" in codes
    assert "ARTIFACT_MAPPING_MISSING" in codes
    mapping_finding = next(finding for finding in report.findings if finding.code == "ARTIFACT_MAPPING_MISSING")
    assert "Paper 2" in mapping_finding.value


def test_collection_contract_rejects_required_item_value_mismatch(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs/docs"
    outputs.mkdir(parents=True)
    (outputs / "report.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")
    (outputs / "report.md").write_text("# 翻译稿\n\nPaper 1\nPaper 2\n", encoding="utf-8")
    (outputs / "source_index.json").write_text(
        json.dumps(
            {
                "completion_evidence": {"source": "fixture"},
                "rows": [
                    {"title": "Paper 1", "url": "https://example.com/1", "date": "2026-01-01", "translated": True},
                    {"title": "Paper 2", "url": "https://example.com/2", "date": "2026-02-01", "translated": False},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=outputs / "report.pdf",
            workspace_root=tmp_path,
            validation_contract={
                "validator": "document_acceptance",
                "collection_contract": {
                    "source_json_ref": "outputs/docs/source_index.json",
                    "items_path": "rows",
                    "min_items_total": 2,
                    "required_item_fields": ["title", "url", "date", "translated"],
                    "required_item_values": {"translated": True},
                    "require_completion_evidence": True,
                    "mapping": {
                        "artifact_ref": "outputs/docs/report.md",
                        "key_fields": ["title"],
                        "min_mapped_items": 2,
                    },
                },
            },
        )
    )

    assert report.ok is True
    assert "COLLECTION_ITEM_VALUE_MISMATCH" in {finding.code for finding in report.findings}


def test_collection_contract_rejects_item_date_before_declared_min(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs/docs"
    outputs.mkdir(parents=True)
    (outputs / "report.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")
    (outputs / "report.md").write_text("# 翻译稿\n\nPaper 2024\nPaper 2025\n", encoding="utf-8")
    (outputs / "source_index.json").write_text(
        json.dumps(
            {
                "completion_evidence": {"source": "fixture"},
                "rows": [
                    {"title": "Paper 2024", "url": "https://example.com/2024", "date": "2024-12-01", "translated": True},
                    {"title": "Paper 2025", "url": "https://example.com/2025", "date": "2025-01-01", "translated": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=outputs / "report.pdf",
            workspace_root=tmp_path,
            validation_contract={
                "validator": "document_acceptance",
                "collection_contract": {
                    "source_json_ref": "outputs/docs/source_index.json",
                    "items_path": "rows",
                    "min_items_total": 2,
                    "required_item_fields": ["title", "url", "date", "translated"],
                    "required_item_values": {"translated": True},
                    "item_date_bounds": {"field": "date", "min": "2025-01-01"},
                    "require_completion_evidence": True,
                    "mapping": {
                        "artifact_ref": "outputs/docs/report.md",
                        "key_fields": ["title"],
                        "min_mapped_items": 2,
                    },
                },
            },
        )
    )

    assert report.ok is True
    assert "COLLECTION_ITEM_DATE_BEFORE_MIN" in {finding.code for finding in report.findings}


def test_collection_contract_rejects_required_item_placeholder_values(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs/docs"
    outputs.mkdir(parents=True)
    (outputs / "report.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")
    (outputs / "report.md").write_text("# 翻译稿\n\nPaper 1\n", encoding="utf-8")
    (outputs / "source_index.json").write_text(
        json.dumps(
            {
                "completion_evidence": {"source": "fixture"},
                "rows": [
                    {"title": "Paper 1", "url": "https://example.com/1", "date": "__FILL_3_date__", "translated": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=outputs / "report.pdf",
            workspace_root=tmp_path,
            validation_contract={
                "validator": "document_acceptance",
                "collection_contract": {
                    "source_json_ref": "outputs/docs/source_index.json",
                    "items_path": "rows",
                    "min_items_total": 1,
                    "required_item_fields": ["title", "url", "date", "translated"],
                    "required_item_values": {"translated": True},
                    "require_completion_evidence": True,
                    "mapping": {
                        "artifact_ref": "outputs/docs/report.md",
                        "key_fields": ["title"],
                        "min_mapped_items": 1,
                    },
                },
            },
        )
    )

    assert report.ok is True
    placeholder = next(finding for finding in report.findings if finding.code == "COLLECTION_ITEM_PLACEHOLDER_VALUE")
    assert placeholder.location == "outputs/docs/source_index.json#0:date"
    assert "__FILL_3_date__" in placeholder.value


def test_collection_contract_accepts_sheets_rows_when_flat_items_path_is_missing(tmp_path: Path) -> None:
    source_id = "src-web"
    (tmp_path / "source_index.json").write_text(
        json.dumps(
            {
                "completion_evidence": {"method": "api_json_collection"},
                "sheets": [
                    {
                        "name": "web_search",
                        "rows": [
                            {
                                "date": "2026-01-01",
                                "field_source_ids": {"date": [source_id], "title": [source_id], "url": [source_id]},
                                "title": "Paper 1",
                                "translated": True,
                                "url": "https://example.com/1",
                            },
                            {
                                "date": "2026-02-01",
                                "field_source_ids": {"date": [source_id], "title": [source_id], "url": [source_id]},
                                "title": "Paper 2",
                                "translated": True,
                                "url": "https://example.com/2",
                            },
                        ],
                    }
                ],
                "source_refs": [_source_ref(source_id)],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = collection_contract_findings(
        {
            "collection_contract": {
                "source_json_ref": "source_index.json",
                "items_path": "rows",
                "min_items_total": 2,
                "required_item_fields": ["title", "url", "date", "translated"],
                "required_item_values": {"translated": True},
                "require_completion_evidence": True,
                "required_item_evidence_fields": ["title", "url", "date"],
            }
        },
        tmp_path,
    )

    assert findings == []


def test_collection_contract_rejects_global_claims_without_row_field_sources(tmp_path: Path) -> None:
    _write_source_workbook(tmp_path, {"sheets": [_sheet("week-1", 2)], "source_refs": [_source_ref("src-1")], "claims": [_claim("指标值", "src-1")]})
    report = _validate_row_evidence_workbook(tmp_path, min_items=2)

    assert report.ok is True
    assert "COLLECTION_ITEM_EVIDENCE_FIELD_MISSING" in {finding.code for finding in report.findings}


def test_collection_contract_accepts_row_field_sources(tmp_path: Path) -> None:
    _write_source_workbook(
        tmp_path,
        {"sheets": [_sheet("week-1", 2, source_id="src-1")], "source_refs": [_source_ref("src-1")], "claims": [_claim("指标值", "src-1")]},
    )
    report = _validate_row_evidence_workbook(tmp_path, min_items=2)

    assert report.ok, report.to_dict()


def test_collection_contract_rejects_unaudited_row_field_source(tmp_path: Path) -> None:
    _write_source_workbook(
        tmp_path,
        {
            "claims": [_claim("指标值", "src-1")],
            "sheets": [_sheet("week-1", 1, source_id="src-1")],
            "source_refs": [{"source_id": "src-1", "status": "AVAILABLE", "uri": "https://example.com"}],
        },
    )
    report = _validate_row_evidence_workbook(tmp_path, min_items=1)

    assert report.ok is True
    assert "COLLECTION_ITEM_EVIDENCE_SOURCE_UNAUDITED" in {finding.code for finding in report.findings}


def test_collection_contract_rejects_content_hash_without_source_binding(tmp_path: Path) -> None:
    _write_source_workbook(
        tmp_path,
        {
            "claims": [_claim("指标值", "src-1")],
            "sheets": [_sheet("week-1", 1, source_id="src-1")],
            "source_refs": [
                {
                    "content_sha256": "abc123",
                    "source_id": "src-1",
                    "status": "AVAILABLE",
                    "uri": "https://example.com",
                }
            ],
        },
    )
    report = _validate_row_evidence_workbook(tmp_path, min_items=1)

    assert report.ok is True
    assert "COLLECTION_ITEM_EVIDENCE_SOURCE_UNAUDITED" in {finding.code for finding in report.findings}


def test_collection_contract_accepts_row_scoped_claims(tmp_path: Path) -> None:
    _write_source_workbook(
        tmp_path,
        {"sheets": [_sheet("week-1", 1)], "source_refs": [_source_ref("src-1")], "claims": [_row_claim("指标值", 0, "src-1")]},
    )
    report = _validate_row_evidence_workbook(tmp_path, min_items=1)

    assert report.ok, report.to_dict()


def _write_source_workbook(tmp_path: Path, payload: dict[str, object]) -> None:
    (tmp_path / "source_data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    write_xlsx_fixture(tmp_path, {"path": "report.xlsx", "source_json_path": "source_data.json"})


def _validate_row_evidence_workbook(tmp_path: Path, *, min_items: int):
    return validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["记录名", "地址", "指标值"],
                "staging_contract": {"source_json_ref": "source_data.json"},
                "evidence_contract": {"required_fields": ["指标值"], "require_verified": True},
                "collection_contract": _row_evidence_collection_contract(min_items),
            },
        )
    )


def _row_evidence_collection_contract(min_items: int) -> dict[str, object]:
    return {
        "source_json_ref": "source_data.json",
        "groups_path": "sheets",
        "items_path": "rows",
        "min_groups": 1,
        "min_items_per_group": min_items,
        "required_item_fields": ["记录名", "地址", "指标值"],
        "require_item_evidence": True,
        "required_item_evidence_fields": ["指标值"],
    }


def _sheet(name: str, count: int, *, source_id: str = "") -> dict[str, object]:
    return {
        "name": name,
        "columns": ["记录名", "地址", "指标值"],
        "rows": [
            {
                "记录名": f"repo-{index}",
                "地址": f"https://example.com/repo-{index}",
                "指标值": index,
                **({"field_source_ids": {"指标值": [source_id]}} if source_id else {}),
            }
            for index in range(count)
        ],
    }


def _source_ref(source_id: str) -> dict[str, object]:
    return {
        "content_sha256": "abc123",
        "source_id": source_id,
        "uri": "https://example.com",
        "status": "AVAILABLE",
        "reserved": {"tool_call_id": f"call-{source_id}"},
    }


def _claim(field: str, source_id: str) -> dict[str, object]:
    return {"claim_id": f"claim-{field}", "field": field, "source_ids": [source_id], "verification_status": "VERIFIED"}


def _row_claim(field: str, value: object, source_id: str) -> dict[str, object]:
    return {**_claim(field, source_id), "value": value, "reserved": {"item_path": "sheets[0].rows[0]"}}
