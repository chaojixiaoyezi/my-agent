"""Focused tests for artifact acceptance validators."""

from __future__ import annotations


# LLM: HTML validator should catch placeholder links that models often call "working" by mistake.
# 函数用途: 验证 HTML 产物验收能发现 `href="#"` 这类假按钮/假链接，不能只相信模型自检。
def test_html_acceptance_flags_placeholder_links(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text('<html><body><a href="#">More</a></body></html>', encoding="utf-8")

    report = validate_html_artifact(ArtifactAcceptanceRequest(path=path))

    assert report.ok is False
    assert any(item.code == "HTML_PLACEHOLDER_LINK" for item in report.findings)


# LLM: HTML validator should distinguish image refs from unrelated external resources like fonts.
# 函数用途: 验证 HTML 图片坏链风险会被标记，但 Google Fonts 这类非图片链接不会误判为图片坏链。
def test_html_acceptance_flags_external_images_without_flagging_fonts(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text(
        '<html><head><link href="https://fonts.googleapis.com/css2?family=Inter"></head>'
        '<body><img src="https://example.com/hero.jpg"></body></html>',
        encoding="utf-8",
    )

    report = validate_html_artifact(ArtifactAcceptanceRequest(path=path))

    codes = [item.code for item in report.findings]
    assert "HTML_EXTERNAL_IMAGE_REF" in codes
    assert "HTML_EXTERNAL_STYLESHEET" not in codes


# LLM: Acceptance reports must be JSON friendly for future QA and repair agents.
# 函数用途: 确认验收报告可以作为结构化 findings 传给修复流程，不需要解析自然语言。
def test_html_acceptance_report_to_dict(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text("<html><body><a href=\"#\">Bad</a></body></html>", encoding="utf-8")

    payload = validate_html_artifact(ArtifactAcceptanceRequest(path=path)).to_dict()

    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "HTML_PLACEHOLDER_LINK"


# LLM: Generic artifact acceptance should route common formats through one contract.
# 函数用途: 验证 JSON/CSV/XLSX/PDF 等常见产物不再各走各的验收入口，后续 QA 可以统一消费报告。
def test_validate_artifact_routes_common_formats(tmp_path):
    from zipfile import ZIP_DEFLATED, ZipFile

    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    json_path = tmp_path / "report.json"
    json_path.write_text('{"ok": true}', encoding="utf-8")
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("name,value\nA,1\n", encoding="utf-8")
    xlsx_path = tmp_path / "report.xlsx"
    with ZipFile(xlsx_path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", "<Types></Types>")
        workbook.writestr("xl/workbook.xml", "<workbook></workbook>")
        workbook.writestr("xl/worksheets/sheet1.xml", "<worksheet></worksheet>")
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")

    reports = [
        validate_artifact(ArtifactAcceptanceRequest(path=json_path)),
        validate_artifact(ArtifactAcceptanceRequest(path=csv_path)),
        validate_artifact(ArtifactAcceptanceRequest(path=xlsx_path)),
        validate_artifact(ArtifactAcceptanceRequest(path=pdf_path)),
    ]

    assert all(report.ok for report in reports)
    assert {report.artifact_kind for report in reports} == {"json", "csv", "xlsx", "pdf"}


# LLM: Generic validators should turn broken files into repairable machine findings.
# 函数用途: 验证坏 JSON 会产生稳定错误码，而不是只在 CLI 输出一段自然语言。
def test_validate_artifact_reports_invalid_json(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "broken.json"
    path.write_text("{bad json", encoding="utf-8")

    report = validate_artifact(ArtifactAcceptanceRequest(path=path))

    assert report.ok is False
    assert report.artifact_kind == "json"
    assert report.findings[0].code == "JSON_INVALID"
