from __future__ import annotations

import hashlib
from pathlib import Path

from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_artifacts import (
    _validate_artifact_item,
)
from agent_py_agent.agent.contracts.artifact_collection_mapping import mapping_findings
from agent_py_agent.agent.contracts.artifact_format_lint import lint_artifact_format
from agent_py_agent.agent.contracts.gates.artifact_provenance import (
    evaluate_artifact_provenance_gate,
)
from agent_py_agent.agent.contracts.gates.document_content_quality import (
    evaluate_document_content_quality_gate,
)


# LLM: V3 document quality must reject thin sections through structured facts, not a PDF-specific rule.
# 函数用途: 验证文档类交付物即使章节存在，也必须满足合同声明的正文量和占位比例。
def test_document_content_quality_rejects_thin_required_sections(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# 摘要\n这是一段足够的摘要内容。\n\n# 结果\n待补充\n", encoding="utf-8")

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


# LLM: HTML documents use the same content gate, so heading extraction must not be format fragile.
# 函数用途: 验证 HTML 标题和正文会被抽成统一文档事实，而不是因为格式适配器错误漏检。
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


# LLM: Source coverage levels must not treat title-only mentions as body coverage.
# 函数用途: 验证 source item 只在产物里出现标题时，只能算 metadata_only，不能满足 body 合同。
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


# LLM: Builder provenance must prove final artifacts came from the latest source hash.
# 函数用途: 验证本轮写过文件还不够，source/checkpoint 更新后旧构建产物必须进入返工。
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


# LLM: Format lint is a unified entrypoint over existing validators, not another task-specific gate.
# 函数用途: 验证统一 lint 入口能复用现有 JSON 格式验收并输出同一 finding 形状。
def test_artifact_format_lint_reuses_existing_format_validators(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{bad json", encoding="utf-8")

    report = lint_artifact_format(path=broken, workspace_root=tmp_path, validation_contract={})

    assert report.ok is False
    assert report.artifact_kind == "json"
    assert [finding.code for finding in report.findings] == ["JSON_INVALID"]


# LLM: Closeout must consume the unified lint path so V3 gates protect real delivery, not only direct tests.
# 函数用途: 验证交付收口入口会把 document_quality_contract 失败转成机器 finding，驱动通用返工。
def test_closeout_artifact_validation_uses_v3_document_quality(tmp_path: Path) -> None:
    report = tmp_path / "handoff.md"
    report.write_text("# 摘要\n很好。\n\n# 结果\n待补充\n", encoding="utf-8")

    artifact = _validate_artifact_item(
        {
            "artifact_id": "handoff",
            "kind": "md",
            "path": "handoff.md",
            "validation_contract": {
                "document_quality_contract": {
                    "required_sections": ["摘要", "结果"],
                    "min_chars_per_section": 10,
                    "max_placeholder_ratio": 0.05,
                }
            },
        },
        tmp_path,
        archive_tool_calls=[],
        run_id="run-closeout",
    )

    assert artifact["ok"] is False
    codes = [finding["code"] for finding in artifact["acceptance_report"]["findings"]]
    assert "DOCUMENT_SECTION_TOO_THIN" in codes
    assert "DOCUMENT_PLACEHOLDER_RATIO_EXCEEDED" in codes
