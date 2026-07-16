from __future__ import annotations

import hashlib
from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
)
from agent_py_agent.agent.contracts.artifact_collection_mapping import mapping_findings
from agent_py_agent.agent.contracts.gates.artifact_provenance import (
    evaluate_artifact_provenance_gate,
)
from agent_py_agent.agent.contracts.gates.document_content import (
    evaluate_document_content_quality_gate,
)


def test_document_content_quality_rejects_thin_required_sections(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 摘要\n这是一段足够的摘要内容。\n\n# 结果\n__FILL__\n", encoding="utf-8")

    decision = evaluate_document_content_quality_gate(
        artifact_ref=str(report),
        workspace_root=tmp_path,
        contract={
            "required_sections": ["摘要", "结果"],
            "min_chars_per_section": 12,
            "max_placeholder_ratio": 0.05,
        },
    )

    assert decision.allowed is False
    assert "DOCUMENT_SECTION_TOO_THIN" in decision.finding_codes
    assert "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED" in decision.finding_codes
    action = decision.to_dict()["recovery"]["actions"][0]
    assert action["evidence"]["current_state"]
    assert action["evidence"]["required_state"]


def test_document_content_quality_reads_html_sections(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<html><body><h1>摘要</h1><p>这里是完整的摘要正文，足够通过章节厚度检查。</p></body></html>",
        encoding="utf-8",
    )

    decision = evaluate_document_content_quality_gate(
        artifact_ref=str(report),
        workspace_root=tmp_path,
        contract={"required_sections": ["摘要"], "min_chars_per_section": 12},
    )

    assert decision.allowed is True
    assert not decision.finding_codes


def test_source_item_coverage_rejects_metadata_only_mapping(tmp_path: Path) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("# 资料\n\nPaper A\n\nPaper B\n", encoding="utf-8")
    items = [{"title": "Paper A"}, {"title": "Paper B"}]

    findings = mapping_findings(
        items,
        {
            "mapping": {
                "artifact_ref": "report.md",
                "key_fields": ["title"],
                "min_mapped_items": 2,
                "min_coverage_level": "body",
                "min_body_chars": 80,
            }
        },
        "source_index.json",
        tmp_path,
    )

    assert [finding.code for finding in findings] == ["ARTIFACT_MAPPING_COVERAGE_TOO_SHALLOW"]
    assert "metadata_only" in findings[0].value


def test_artifact_provenance_rejects_stale_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "draft.md"
    source.write_text("# 新稿\n\n这是最新正文。\n", encoding="utf-8")
    final = tmp_path / "final.pdf"
    final.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n")

    decision = evaluate_artifact_provenance_gate(
        {
            "ok": True,
            "path": str(final),
            "provenance": {
                "ok": True,
                "run_id": "run-1",
                "tool_name": "document_builder",
                "operation_id": "op-build",
                "idempotency_key": "build-final",
                "created_by_current_run": True,
                "source_artifact_hashes": {str(source): "sha256:old-source"},
                "build_input_hash": "sha256:old-source",
                "build_output_hash": f"sha256:{hashlib.sha256(final.read_bytes()).hexdigest()}",
            },
        },
        run_id="run-1",
    )

    assert decision.allowed is False
    assert "BUILDER_PROVENANCE_STALE" in decision.finding_codes
    assert decision.to_dict()["recovery"]["actions"][0]["evidence"]["current_state"]


def test_artifact_acceptance_reuses_existing_format_validators(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{bad json", encoding="utf-8")

    report = validate_artifact(
        ArtifactAcceptanceRequest(path=broken, workspace_root=tmp_path, validation_contract={})
    )

    assert report.ok is False
    assert report.artifact_kind == "json"
    assert [finding.code for finding in report.findings] == ["JSON_INVALID"]


def test_document_content_quality_uses_contract_tokens_for_plain_language_placeholders(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 摘要\n这是一段足够的摘要内容。\n\n# 结果\n待补充\n", encoding="utf-8")

    default_decision = evaluate_document_content_quality_gate(
        artifact_ref=str(report),
        workspace_root=tmp_path,
        contract={
            "required_sections": ["摘要", "结果"],
            "min_chars_per_section": 2,
            "max_placeholder_ratio": 0.05,
        },
    )
    explicit_decision = evaluate_document_content_quality_gate(
        artifact_ref=str(report),
        workspace_root=tmp_path,
        contract={
            "required_sections": ["摘要", "结果"],
            "min_chars_per_section": 2,
            "max_placeholder_ratio": 0.05,
            "placeholder_tokens": ["待补充"],
        },
    )

    assert "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED" not in default_decision.finding_codes
    assert "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED" in explicit_decision.finding_codes
