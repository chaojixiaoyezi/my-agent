from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool


# LLM: complex table tasks need coverage contracts, not just workbook existence.
# 函数用途: 验证结构化分组数量不足时，即使 xlsx 存在且列正确，也不能通过验收。
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
                "claims": [_claim("项目名", "src-1")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = DataWorkbookTool(tmp_path)
    assert tool.execute({"path": "report.xlsx", "source_json_path": "source_data.json"}).ok

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["项目名", "地址", "上升 star 数"],
                "staging_contract": {"source_json_ref": "source_data.json"},
                "collection_contract": {
                    "source_json_ref": "source_data.json",
                    "groups_path": "sheets",
                    "items_path": "rows",
                    "min_groups": 20,
                    "min_items_per_group": 10,
                    "required_item_fields": ["项目名", "地址", "上升 star 数"],
                },
            },
        )
    )

    assert not report.ok
    assert "COLLECTION_TOO_FEW_GROUPS" in {finding.code for finding in report.findings}


# LLM: each declared bucket must satisfy item-count contracts independently.
# 函数用途: 验证某个分组行数不足会被机器验收发现，防止只做最近几条数据也通过。
def test_collection_contract_rejects_group_with_too_few_items(tmp_path: Path) -> None:
    source = tmp_path / "source_data.json"
    source.write_text(
        json.dumps(
            {
                "sheets": [_sheet("week-1", 10), _sheet("week-2", 2)],
                "source_refs": [_source_ref("src-1")],
                "claims": [_claim("项目名", "src-1")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = DataWorkbookTool(tmp_path)
    assert tool.execute({"path": "report.xlsx", "source_json_path": "source_data.json"}).ok

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["项目名", "地址", "上升 star 数"],
                "staging_contract": {"source_json_ref": "source_data.json"},
                "collection_contract": {
                    "source_json_ref": "source_data.json",
                    "groups_path": "sheets",
                    "items_path": "rows",
                    "min_groups": 2,
                    "min_items_per_group": 10,
                    "required_item_fields": ["项目名", "地址", "上升 star 数"],
                },
            },
        )
    )

    assert not report.ok
    assert "COLLECTION_GROUP_TOO_FEW_ITEMS" in {finding.code for finding in report.findings}


# LLM: document outputs need source-index completeness and source-to-artifact mapping.
# 函数用途: 验证 PDF 签名正确但来源数量、完整性证据和正文映射不足时不能通过验收。
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
    assert not report.ok
    assert "COLLECTION_TOO_FEW_ITEMS" in codes
    assert "COLLECTION_COMPLETENESS_EVIDENCE_MISSING" in codes
    assert "ARTIFACT_MAPPING_MISSING" in codes
    mapping_finding = next(finding for finding in report.findings if finding.code == "ARTIFACT_MAPPING_MISSING")
    assert "Paper 2" in mapping_finding.value


# LLM: row-level machine fields must satisfy the declared contract, not only exist.
# 函数用途: 验证清单行里的布尔/枚举等结构化状态不符合要求时会失败，避免 translated=false 也被当成完成。
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

    assert not report.ok
    assert "COLLECTION_ITEM_VALUE_MISMATCH" in {finding.code for finding in report.findings}


def _sheet(name: str, count: int) -> dict[str, object]:
    return {
        "name": name,
        "columns": ["项目名", "地址", "上升 star 数"],
        "rows": [
            {
                "项目名": f"repo-{index}",
                "地址": f"https://example.com/repo-{index}",
                "上升 star 数": index,
            }
            for index in range(count)
        ],
    }


def _source_ref(source_id: str) -> dict[str, str]:
    return {"source_id": source_id, "uri": "https://example.com", "status": "AVAILABLE"}


def _claim(field: str, source_id: str) -> dict[str, object]:
    return {"claim_id": f"claim-{field}", "field": field, "source_ids": [source_id], "verification_status": "VERIFIED"}
