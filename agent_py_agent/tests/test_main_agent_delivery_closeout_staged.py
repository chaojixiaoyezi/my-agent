"""LLM: staged recovery tests for main-agent delivery closeout contracts."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    xlsx_delivery_contract,
)


# LLM: Contract-driven recovery actions should prefer fixing empty staged JSON before invoking any builder tool.
# 函数用途: 验证 source_data.json 为空时，closeout 先产出 STAGED_JSON_NO_ROWS，而不会误导模型直接调 builder。
def test_delivery_closeout_prefers_checkpoint_quality_actions_before_builder():
    actions = _actions_for_source('{"weekly_top10": []}')

    assert "STAGED_JSON_NO_ROWS" in actions
    assert actions["STAGED_JSON_NO_ROWS"]["recommended_action"] == "write_non_empty_structured_rows"
    assert actions["STAGED_JSON_NO_ROWS"]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
    assert actions["STAGED_JSON_NO_ROWS"]["writer_tool"] == "write_structured_json"
    assert "checkpoint_shape_hint" in actions["STAGED_JSON_NO_ROWS"]
    assert actions["STAGED_JSON_NO_ROWS"]["required_columns"] == ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"]
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Skeleton-only staged JSON must stay in "fill data first" mode instead of pretending the builder can run.
# 函数用途: 验证只有 sheet/meta 骨架、没有真实行数据时，closeout 仍返回 STAGED_JSON_NO_ROWS。
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


# LLM: Required tabular columns should force staged JSON into a builder-compatible table shape.
# 函数用途: 验证 weeks/projects 这类非表格结构不会在声明 required_columns 时被误判为 builder-ready。
def test_delivery_closeout_rejects_non_tabular_checkpoint_when_columns_required():
    actions = _actions_for_source(
        json.dumps({"weeks": [{"week_id": 1, "projects": [{"name": "demo", "url": "https://example.com/demo"}]}]}, ensure_ascii=False)
    )

    assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
    assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"] == "项目名,地址,上升 star 数,中文解释,推荐理由"
    assert "checkpoint_shape_hint" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Staged workbook JSON must satisfy contract-declared columns before builder execution.
# 函数用途: 验证 required_columns 会在 source_data.json 阶段提前检查，避免错列名数据进入 xlsx builder。
def test_delivery_closeout_requires_staged_json_required_columns_before_builder():
    actions = _actions_for_source(
        json.dumps(
            {
                "sheets": [
                    {
                        "name": "week-1",
                        "columns": ["项目名", "地址", "周上升star数"],
                        "rows": [{"项目名": "demo", "地址": "https://example.com", "周上升star数": 10}],
                    }
                ]
            },
            ensure_ascii=False,
        )
    )

    assert "STAGED_JSON_REQUIRED_COLUMNS_MISSING" in actions
    assert actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert "上升 star 数" in actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]["missing_columns"]
    assert "STAGING_BUILDER_READY" not in actions


def test_delivery_closeout_requires_staged_json_evidence_before_builder():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["上升 star 数"],
        "require_verified": True,
    }
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in actions
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["recommended_action"] == "repair_evidence_refs"
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["required_fields"] == ["上升 star 数"]
    assert actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["writer_tool"] == "write_structured_json"
    assert "claims" in actions["EVIDENCE_REQUIRED_FIELD_MISSING"]["evidence_shape_hint"]
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Missing source checkpoints with evidence contracts must point at source collection, not empty skeletons.
# 函数用途: 验证缺失的来源型 checkpoint 恢复动作优先要求 api_json_collection 和结构化来源证据。
def test_delivery_closeout_missing_source_checkpoint_prefers_auditable_collection_tool():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["collection_contract"] = {
        "source_json_ref": "outputs/github_star_growth/source_data.json",
        "required_item_evidence_fields": ["项目名", "地址", "上升 star 数"],
        "require_completion_evidence": True,
        "require_item_evidence": True,
    }
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["项目名", "地址", "上升 star 数"],
        "require_verified": True,
    }

    with tempfile.TemporaryDirectory() as td:
        _, actions = _enriched_report(Path(td).resolve(), contract)

    action = actions["STAGING_CHECKPOINT_MISSING"]
    assert action["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
    assert action["checkpoint_materialization_mode"] == "source_evidence_first"
    assert action["requires_auditable_source_evidence"] is True
    assert action["writer_tool"] == "api_json_collection"
    assert action["write_tools"] == ["api_json_collection", "write_structured_json"]
    assert "source_refs" in action["required_structured_fields"]
    assert "claims" in action["required_structured_fields"]


# LLM: Evidence recovery should carry every missing required field in one machine action.
# 函数用途: 验证 closeout 不会只暴露第一个缺证据字段，避免下一轮模型反复局部修。
def test_delivery_closeout_aggregates_missing_evidence_fields():
    contract = xlsx_delivery_contract()
    artifact = contract["artifacts"][0]
    artifact["validation_contract"]["required_sheets_min"] = 1
    artifact["validation_contract"]["evidence_contract"] = {
        "required_fields": ["项目名", "地址", "上升 star 数"],
        "require_verified": True,
    }
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    action = actions["EVIDENCE_REQUIRED_FIELD_MISSING"]
    assert action["required_fields"] == ["上升 star 数", "地址", "项目名"]
    assert action["writer_tool"] == "write_structured_json"
    assert "source_refs" in action["evidence_shape_hint"]


# LLM: Staged workbook sources should satisfy required sheet-count contracts before the builder runs.
# 函数用途: 验证 source_data.json 只有一个 sheet 时会先修阶段 JSON，而不是生成注定失败的 workbook。
def test_delivery_closeout_requires_staged_json_min_sheet_count_before_builder():
    contract = xlsx_delivery_contract()
    contract["artifacts"][0]["validation_contract"].pop("evidence_contract", None)
    contract["artifacts"][0]["validation_contract"]["required_sheets_min"] = 2
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "STAGED_JSON_TOO_FEW_SHEETS" in actions
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["required_sheets_min"] == 2
    assert actions["STAGED_JSON_TOO_FEW_SHEETS"]["writer_tool"] == "write_structured_json"
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Contract-driven recovery actions should expose builder readiness only after staged JSON is structurally usable.
# 函数用途: 验证 source_data.json 已有非空结构化数据时，closeout 才会生成 invoke_builder_tool 动作。
def test_delivery_closeout_adds_generic_staging_builder_action_for_ready_source():
    contract = xlsx_delivery_contract()
    contract["artifacts"][0]["validation_contract"]["required_sheets_min"] = 1
    actions = _actions_for_source(_valid_rows_json(), contract=contract)

    assert "STAGING_BUILDER_READY" in actions
    assert actions["STAGING_BUILDER_READY"]["recommended_action"] == "invoke_builder_tool"
    assert actions["STAGING_BUILDER_READY"]["builder_tool"] == "data_to_workbook"
    assert actions["STAGING_BUILDER_READY"]["source_ref"] == "outputs/github_star_growth/source_data.json"


# LLM: Failed builder outputs should be regenerated from the staged source instead of manually patched.
# 函数用途: 验证 workbook 已存在但验收失败时，恢复动作仍会给出 data_to_workbook 的 builder 调用合同。
def test_delivery_closeout_adds_builder_action_for_failed_existing_workbook():
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        _write_source(workspace, _valid_rows_json())
        result = DataWorkbookTool(workspace).execute(
            {
                "path": "outputs/github_star_growth/github_star_growth.xlsx",
                "sheets": [
                    {
                        "name": "week-1",
                        "columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
                        "rows": [
                            {
                                "项目名": "",
                                "地址": "https://example.com",
                                "上升 star 数": 10,
                                "中文解释": "demo",
                                "推荐理由": "demo",
                            }
                        ],
                    }
                ],
            }
        )
        assert result.ok

        _, actions = _enriched_report(workspace, contract)

        assert actions["STAGING_BUILDER_READY"]["recommended_action"] == "invoke_builder_tool"
        assert actions["STAGING_BUILDER_READY"]["builder_tool"] == "data_to_workbook"
        assert actions["STAGING_BUILDER_READY"]["source_ref"] == "outputs/github_star_growth/source_data.json"
        assert actions["STAGING_BUILDER_READY"]["output_ref"] == "outputs/github_star_growth/github_star_growth.xlsx"


# LLM: Invalid staged JSON should produce a repair action with parse context instead of a builder action.
# 函数用途: 验证阶段 JSON 损坏时，closeout 会要求先修 JSON，而不是继续下游构建。
def test_delivery_closeout_reports_invalid_checkpoint_json():
    actions = _actions_for_source('[{"项目名":"demo","地址":"https://example.com"')

    assert "STAGED_JSON_INVALID" in actions
    assert actions["STAGED_JSON_INVALID"]["recommended_action"] == "repair_structured_checkpoint_json"
    assert actions["STAGED_JSON_INVALID"]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
    assert "parse_error" in actions["STAGED_JSON_INVALID"]
    assert actions["STAGED_JSON_INVALID"]["writer_tool"] == "write_structured_json"
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Builder outputs should be built by the declared builder, not treated as manual checkpoints while the source is bad.
# 函数用途: 验证 source checkpoint 未修好时，恢复动作不会再要求手工物化 workbook/pdf 这类 builder output。
def test_delivery_closeout_does_not_materialize_builder_output_before_source_is_ready():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        report, _ = _enriched_report_for_source(workspace, '[{"项目名":"demo","地址":"https://example.com"', contract)
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


# LLM: Repeated staged JSON failures should route into repair guard before no-progress closeout blocks.
# 函数用途: 验证 JSON 阶段文件损坏时，即使失败重复，也先给结构化修复链路接管机会。
def test_delivery_closeout_defers_no_progress_block_when_write_first_repair_exists():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = xlsx_delivery_contract()
        report, actions = _enriched_report_for_source(workspace, '[{"项目名":"demo","地址":"https://example.com"', contract)
        previous = _previous_delivery_progress(report, unchanged_failure_count=5)

        from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
            _enrich_delivery_progress,
            _should_block_on_no_progress,
        )

        enriched = _enrich_delivery_progress(report, previous, workspace, contract=contract)

        assert "STAGED_JSON_INVALID" in actions
        assert enriched["delivery_progress"]["unchanged_failure_count"] >= 6
        assert _should_block_on_no_progress(enriched, contract=contract, workspace_root=workspace) is False


# LLM: Delivery progress must watch contract-declared artifact roots, not only built-in outputs/.
# 函数用途: 复现 Live Lab 使用 lab_outputs 时，真实文件变化曾被误判为无进展而过早阻断的问题。
def test_delivery_progress_tracks_contract_declared_artifact_roots():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _lab_outputs_web_contract()
        report = _failed_lab_outputs_web_report(workspace)

        from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
            _enrich_delivery_progress,
        )

        first = _enrich_delivery_progress(report, {}, workspace, contract=contract)
        target = workspace / "lab_outputs/main-web-app/index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("<!doctype html><html><body><input id='email'></body></html>", encoding="utf-8")
        second = _enrich_delivery_progress(report, first, workspace, contract=contract)

        assert second["delivery_progress"]["unchanged_failure_count"] == 1


# LLM: A valid final file cannot bypass invalid staged checkpoints declared by the same contract.
# 函数用途: 验证 PDF 签名有效但 source_index.json 为空时，closeout 仍按阶段合同失败，不误发完成标记。
def test_delivery_closeout_rejects_valid_final_artifact_when_staged_checkpoint_is_empty():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        draft = workspace / "outputs/research_documents/research_documents_zh.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("# 翻译正文\n\n这是已经生成的中文草稿。", encoding="utf-8")
        source_index = workspace / "outputs/research_documents/source_index.json"
        source_index.parent.mkdir(parents=True, exist_ok=True)
        source_index.write_text("[]", encoding="utf-8")

        report, actions = _enriched_report(workspace, contract)
        findings = report["artifacts"][0]["acceptance_report"]["findings"]

        assert report["ok"] is False
        assert {item["code"] for item in findings} == {"STAGED_JSON_NO_ROWS"}
        assert actions["STAGED_JSON_NO_ROWS"]["recommended_action"] == "write_non_empty_structured_rows"
        assert actions["STAGED_JSON_NO_ROWS"]["checkpoint_ref"] == "outputs/research_documents/source_index.json"


# LLM: Non-JSON staged sources should still become builder-ready once present and non-empty.
# 函数用途: 验证 Markdown 草稿存在且 PDF 缺失时，恢复动作会指向通用 markdown_to_pdf builder。
def test_delivery_closeout_adds_document_builder_action_for_ready_markdown_source():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        source_index = workspace / "outputs/research_documents/source_index.json"
        source_index.parent.mkdir(parents=True, exist_ok=True)
        source_index.write_text('[{"title":"demo","url":"https://example.com"}]', encoding="utf-8")
        draft = workspace / "outputs/research_documents/research_documents_zh.md"
        draft.write_text("# 翻译正文\n\n这是已经完成的中文草稿。", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        assert "STAGING_BUILDER_READY" in actions
        assert actions["STAGING_BUILDER_READY"]["builder_tool"] == "markdown_to_pdf"
        assert actions["STAGING_BUILDER_READY"]["source_ref"] == "outputs/research_documents/research_documents_zh.md"
        assert actions["STAGING_BUILDER_READY"]["output_ref"] == "outputs/research_documents/research_documents_zh.pdf"


# LLM: Evidence repair for document flows must target the structured source index, not the markdown builder input.
# 函数用途: 验证 source_markdown_ref 作为 builder 输入时，collection_contract.source_json_ref 仍能生成证据修复动作。
def test_delivery_closeout_repairs_collection_source_evidence_before_document_builder():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        artifact = contract["artifacts"][0]
        artifact["validation_contract"]["collection_contract"] = {
            "source_json_ref": "outputs/research_documents/source_index.json",
        }
        artifact["validation_contract"]["evidence_contract"] = {
            "required_fields": ["title", "url", "date"],
            "require_verified": True,
        }
        _write_json_file(
            workspace / "outputs/research_documents/source_index.json",
            {
                "rows": [{"title": "demo", "url": "https://example.com", "date": "2026-01-01"}],
                "source_refs": [{"source_id": "src-1", "uri": "https://example.com"}],
                "claims": [{"field": "title", "value": "demo", "source_ids": ["src-1"]}],
            },
        )
        draft = workspace / "outputs/research_documents/research_documents_zh.md"
        draft.write_text("# 翻译正文\n\n这是已经完成的中文草稿。", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["EVIDENCE_REQUIRED_FIELD_MISSING"]
        assert action["recommended_action"] == "repair_evidence_refs"
        assert action["checkpoint_ref"] == "outputs/research_documents/source_index.json"
        assert action["required_fields"] == ["date", "url"]
        assert action["writer_tool"] == "write_structured_json"
        assert "STAGING_BUILDER_READY" not in actions


# LLM: Recovery actions must not drop larger structured validator batches.
# 函数用途: 验证多项 DOM 绑定 finding 会完整进入恢复动作，避免续跑只修前 20 个字段。
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


# LLM: _actions_for_source writes source_data.json and returns closeout recovery actions by code.
# 函数用途: 将 staged JSON 场景压成一个 helper，测试只断言结构化恢复动作。
def _actions_for_source(source_content: str, *, contract: dict[str, object] | None = None) -> dict[str, dict[str, object]]:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        _, actions = _enriched_report_for_source(workspace, source_content, contract or xlsx_delivery_contract())
        return actions


# LLM: _enriched_report_for_source executes artifact validation and progress enrichment for one staged source file.
# 函数用途: 复用真实 closeout helper，避免测试自己模拟 recovery action 结果。
def _enriched_report_for_source(
    workspace: Path,
    source_content: str,
    contract: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
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
    enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
    actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}
    return enriched, actions


# LLM: _enriched_report runs the same validation/progress path used by closeout.
# 函数用途: 对任意交付合同执行真实 closeout 验收 helper，测试不重复实现验收逻辑。
def _enriched_report(
    workspace: Path,
    contract: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
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
    enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
    actions = {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}
    return enriched, actions


# LLM: _lab_outputs_web_contract mirrors Live Lab's non-default artifact root without copying its prompt.
# 函数用途: 构造使用 lab_outputs 的结构化网页合同，验证进展指纹按合同根目录扩展。
def _lab_outputs_web_contract() -> dict[str, object]:
    return {
        "case_id": "main_direct_web_app",
        "bootstrap_contract": {
            "materialization_targets": [
                {"workspace_relative_path": f"lab_outputs/main-web-app/{name}"}
                for name in ("index.html", "styles.css", "app.js", "README.md")
            ]
        },
        "artifacts": [
            {
                "artifact_id": "main_web_app_root",
                "kind": "web_project",
                "preferred_path": "lab_outputs/main-web-app",
                "required": True,
                "validation_contract": {"validator": "static_site_check"},
            }
        ],
    }


# LLM: _failed_lab_outputs_web_report keeps the progress test focused on fingerprinting.
# 函数用途: 生成一个稳定失败报告；测试关注文件变化是否重置 unchanged_failure_count。
def _failed_lab_outputs_web_report(workspace: Path) -> dict[str, object]:
    return {
        "case_id": "main_direct_web_app",
        "ok": False,
        "artifacts": [
            {
                "artifact_id": "main_web_app_root",
                "kind": "web_project",
                "path": str(workspace / "lab_outputs/main-web-app"),
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


# LLM: _write_source materializes the staged JSON checkpoint under the task workspace.
# 函数用途: 写 outputs/github_star_growth/source_data.json，让 closeout 读取真实文件状态。
def _write_source(workspace: Path, source_content: str) -> None:
    source = workspace / "outputs/github_star_growth/source_data.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(source_content, encoding="utf-8")


# LLM: _write_valid_pdf writes a minimal signature-valid PDF for closeout tests.
# 函数用途: 让测试聚焦 staged checkpoint 合同，而不是 PDF 解析细节。
def _write_valid_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")


# LLM: _write_json_file writes deterministic fixture JSON for staged contract tests.
# 函数用途: 写入测试用结构化 JSON，避免每个场景手写 JSON 字符串。
def _write_json_file(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: _research_pdf_contract mirrors a generic source-index -> draft -> PDF staged delivery flow.
# 函数用途: 提供非专项的“阶段来源索引 + 最终 PDF”合同，覆盖所有长文档生成任务的收口问题。
def _research_pdf_contract() -> dict[str, object]:
    return {
        "case_id": "document_translation_pdf",
        "artifacts": [
            {
                "artifact_id": "document_pdf",
                "kind": "pdf",
                "preferred_path": "outputs/research_documents/research_documents_zh.pdf",
                "required": True,
                "validation_contract": {
                    "validator": "document_acceptance",
                    "staging_contract": {
                        "strategy": "source_index_then_translation_draft_then_pdf",
                        "builder_tool": "markdown_to_pdf",
                        "source_markdown_ref": "outputs/research_documents/research_documents_zh.md",
                        "pdf_ref": "outputs/research_documents/research_documents_zh.pdf",
                        "checkpoint_shape_hints": {
                            "outputs/research_documents/source_index.json": (
                                '[{"title":"...","authors":["..."],"date":"...","url":"..."}]'
                            )
                        },
                        "checkpoint_refs": [
                            "outputs/research_documents/source_index.json",
                            "outputs/research_documents/research_documents_zh.md",
                            "outputs/research_documents/research_documents_zh.pdf",
                        ],
                    },
                },
            }
        ],
    }


# LLM: _previous_delivery_progress builds a prior closeout report with matching fingerprints.
# 函数用途: 模拟同一失败重复出现，验证 no-progress 与 write-first repair 的交互。
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


# LLM: _valid_rows_json returns a minimal builder-compatible workbook source.
# 函数用途: 提供带必需列和一行数据的 staged JSON。
def _valid_rows_json() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
                    "rows": [
                        {
                            "项目名": "demo",
                            "地址": "https://example.com",
                            "上升 star 数": 10,
                            "中文解释": "demo",
                            "推荐理由": "demo",
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
