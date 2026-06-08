"""Focused tests for artifact acceptance validators."""

from __future__ import annotations


def test_xlsx_acceptance_rejects_package_missing_content_types(tmp_path):
    from zipfile import ZIP_DEFLATED, ZipFile

    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "partial.xlsx"
    with ZipFile(path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("xl/workbook.xml", "<workbook></workbook>")
        workbook.writestr("xl/worksheets/sheet1.xml", "<worksheet></worksheet>")

    report = validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=tmp_path))

    assert report.ok is False
    assert {item.code for item in report.findings} == {"XLSX_INVALID_PACKAGE"}


def test_document_quality_contract_is_advisory_not_blocking(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text("# Summary\n短。\n", encoding="utf-8")

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=tmp_path,
            validation_contract={
                "document_quality_contract": {
                    "required_sections": ["Summary", "Evidence"],
                    "min_chars_per_section": 200,
                    "min_content_units": 5,
                }
            },
        )
    )

    assert report.ok is True
    assert {item.severity for item in report.findings} == {"warning"}
    assert "DOCUMENT_SECTION_MISSING" in {item.code for item in report.findings}


def test_markdown_required_strings_are_blocking(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text("# Report\n\nCP-01 | SECRET-01\n", encoding="utf-8")

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=tmp_path,
            validation_contract={
                "required_strings": ["CP-01", "CP-02"],
                "required_regex": [r"SECRET-01"],
            },
        )
    )

    assert report.ok is False
    assert "ARTIFACT_REQUIRED_TEXT_MISSING" in {item.code for item in report.findings}
    assert [item.value for item in report.findings if item.code == "ARTIFACT_REQUIRED_TEXT_MISSING"] == ["CP-02"]


def test_markdown_required_sections_accept_numbered_headings(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text(
        "# 报告\n\n## 一、架构\n\n架构内容。\n\n## 三、对比\n\n对比内容。\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=tmp_path,
            validation_contract={"required_sections": ["架构", "对比"]},
        )
    )

    assert report.ok is True
    assert "MARKDOWN_REQUIRED_SECTION_MISSING" not in {item.code for item in report.findings}


def test_markdown_trailing_empty_heading_is_blocking(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text("# Report\n\nIntro.\n\n## Next Section", encoding="utf-8")

    report = validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=tmp_path))

    assert report.ok is False
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" in {item.code for item in report.findings}


def test_markdown_unclosed_code_fence_is_blocking(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text("# Report\n\n```python\nprint('half')\n", encoding="utf-8")

    report = validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=tmp_path))

    assert report.ok is False
    assert "MARKDOWN_CODE_FENCE_UNCLOSED" in {item.code for item in report.findings}


def test_markdown_local_reference_check_rejects_missing_tree_file(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    source_root = tmp_path / "all-agent"
    existing = source_root / "openclaude-main" / "src" / "QueryEngine.ts"
    existing.parent.mkdir(parents=True)
    existing.write_text("export class QueryEngine {}\n", encoding="utf-8")
    (source_root / "openclaude-main" / "README.md").write_text("# openclaude\n", encoding="utf-8")

    report_path = tmp_path / "output" / "report.md"
    report_path.parent.mkdir()
    report_path.write_text(
        "# Report\n\n"
        "```\n"
        "openclaude-main/\n"
        "├── src/                  # source files\n"
        "│   ├── QueryEngine.ts    # existing file\n"
        "│   └── Agent.ts          # missing file\n"
        "└── README.md             # existing file\n"
        "```\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=report_path,
            workspace_root=tmp_path,
            reference_roots=(source_root,),
        )
    )

    assert report.ok is False
    missing = [item for item in report.findings if item.code == "MARKDOWN_LOCAL_REF_MISSING"]
    assert [item.value for item in missing] == ["openclaude-main/src/Agent.ts"]


def test_markdown_forbidden_strings_are_blocking_when_contract_declares_them(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    path = tmp_path / "report.md"
    path.write_text("# Report\n\nCP-01\n\n见原文。\n", encoding="utf-8")

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=tmp_path,
            validation_contract={
                "forbidden_strings": ["见原文"],
                "forbidden_regex": [r"CP-\d{2}"],
            },
        )
    )

    assert report.ok is False
    codes = {item.code for item in report.findings}
    assert "ARTIFACT_FORBIDDEN_TEXT_PRESENT" in codes
    assert "ARTIFACT_FORBIDDEN_REGEX_MATCHED" in codes


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


def test_validate_artifact_routes_common_formats(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )
    from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture

    json_path = tmp_path / "report.json"
    json_path.write_text('{"ok": true}', encoding="utf-8")
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("name,value\nA,1\n", encoding="utf-8")
    xlsx_path = write_xlsx_fixture(
        tmp_path,
        "report.xlsx",
        sheets=[{"name": "summary", "rows": [{"name": "A", "value": 1}]}],
    )
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
    assert ref["size_bytes"] == path.stat().st_size


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


def test_validate_artifact_static_site_contract_rejects_missing_required_files(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )

    site = tmp_path / "outputs" / "static_site"
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
