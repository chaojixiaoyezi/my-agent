"""Focused tests for artifact acceptance validators."""

from __future__ import annotations


# LLM: Generic HTML acceptance should focus on structural/resource facts, not task-specific link style rules.
# 函数用途: 验证通用 HTML 验收不会因为 `href="#"` 这类页面实现细节直接判死，只保留通用结构和资源检查。
def test_html_acceptance_does_not_fail_placeholder_links_by_default(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text('<!doctype html><html><head><title>X</title></head><body><a href="#">More</a></body></html>', encoding="utf-8")

    report = validate_html_artifact(ArtifactAcceptanceRequest(path=path))

    assert report.ok is True
    assert report.findings == []


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


# LLM: Contracted single-file HTML should reject remote runtime resources, not only broken images.
# 函数用途: 验证单文件网页合同时，外部字体/CSS/脚本资源会被结构化 finding 拦住。
def test_html_acceptance_contract_rejects_external_resources_for_single_file(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="https://fonts.example/font.css"></head>'
        "<body><main>Furniture</main></body></html>",
        encoding="utf-8",
    )

    report = validate_html_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            validation_contract={"quality_requirements": {"single_file_no_external_assets": True}},
        )
    )

    assert report.ok is False
    assert any(item.code == "HTML_EXTERNAL_RESOURCE_REF" for item in report.findings)


# LLM: Contracted complete HTML should catch truncated files before delivery closeout.
# 函数用途: 验证要求完整 HTML 文档时，缺少 body/html 关闭标签的半截文件不能通过。
def test_html_acceptance_contract_rejects_incomplete_html_document(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text("<!doctype html><html><head><style>body{color:#111}", encoding="utf-8")

    report = validate_html_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            validation_contract={"quality_requirements": {"complete_html_document": True}},
        )
    )

    assert report.ok is False
    assert any(item.code == "HTML_INCOMPLETE_DOCUMENT" for item in report.findings)


# LLM: Acceptance reports must stay JSON friendly even when HTML passes default generic checks.
# 函数用途: 确认验收报告仍可结构化输出，后续 QA/修复链路不需要解析自然语言。
def test_html_acceptance_report_to_dict(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_html_artifact,
    )

    path = tmp_path / "index.html"
    path.write_text("<!doctype html><html><head><title>X</title></head><body><a href=\"#\">Bad</a></body></html>", encoding="utf-8")

    payload = validate_html_artifact(ArtifactAcceptanceRequest(path=path)).to_dict()

    assert payload["ok"] is True
    assert payload["findings"] == []


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


# LLM: CSV data-row strictness should be a structured contract threshold, not a one-size hard stop.
# 函数用途: 验证最终 CSV 默认仍要求数据行，但阶段性表头文件可用 min_data_rows=0 明确放行。
def test_validate_artifact_csv_data_row_threshold_is_contract_driven(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "draft.csv"
    path.write_text("name,value\n", encoding="utf-8")

    strict = validate_artifact(ArtifactAcceptanceRequest(path=path))
    staged = validate_artifact(
        ArtifactAcceptanceRequest(path=path, validation_contract={"min_data_rows": 0})
    )

    assert strict.ok is False
    assert strict.findings[0].code == "CSV_INSUFFICIENT_DATA_ROWS"
    assert staged.ok is True


# LLM: Artifact acceptance reports should expose a structured ArtifactRef, not only a path string.
# 函数用途: 验证产物验收报告包含 artifact_id、path、kind、hash、size，后续恢复和 QA 不用解析自然语言。
def test_validate_artifact_report_contains_structured_artifact_ref(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.json"
    path.write_text('{"ok": true}', encoding="utf-8")

    payload = validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=tmp_path)).to_dict()

    ref = payload["artifact_ref_payload"]
    assert ref["path"] == str(path)
    assert ref["kind"] == "json"
    assert ref["hash"]
    assert ref["reserved"]["size_bytes"] == path.stat().st_size


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


# LLM: Generic binary acceptance should reject known extensions with impossible signatures.
# 函数用途: 验证未知格式的 fallback 也会检查常见二进制签名，避免坏图片/压缩包只因非空而通过。
def test_validate_generic_artifact_checks_known_binary_signatures(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    png_path = tmp_path / "preview.png"
    png_path.write_bytes(b"not-a-real-png")
    zip_path = tmp_path / "bundle.zip"
    zip_path.write_bytes(b"not-a-real-zip")

    png_report = validate_artifact(ArtifactAcceptanceRequest(path=png_path))
    zip_report = validate_artifact(ArtifactAcceptanceRequest(path=zip_path))

    assert png_report.ok is False
    assert png_report.findings[0].code == "ARTIFACT_INVALID_SIGNATURE"
    assert zip_report.ok is False
    assert zip_report.findings[0].code == "ARTIFACT_INVALID_SIGNATURE"


# LLM: Web project directories must be validated by their declared static-site contract, not as generic folders.
# 函数用途: 验证 web_project 目录缺少 validation_contract.required_files 时不能因为目录存在就通过。
def test_validate_artifact_static_site_contract_rejects_missing_required_files(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    site = tmp_path / "outputs" / "shopping_site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><html><body><main id='home'>Shop</main></body></html>",
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=site,
            workspace_root=tmp_path,
            validation_contract={
                "validator": "static_site_check",
                "required_files": ["index.html", "app.js"],
            },
        )
    )

    assert report.ok is False
    assert report.artifact_kind == "web_project"
    assert any(item.code == "STATIC_SITE_MISSING_REQUIRED_FILES" for item in report.findings)
    assert any(item.value == "app.js" for item in report.findings)
