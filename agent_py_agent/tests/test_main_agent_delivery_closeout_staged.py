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


# LLM: Artifact mapping failures should repair the mapped text artifact, not the final binary wrapper.
# 函数用途: 验证 mapping finding 的 location 会成为 repair target，PDF/图片等二进制产物不会误导修复链路。
def test_delivery_closeout_uses_finding_location_as_repair_target_for_mapping_failures():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = {
            "source_json_ref": "outputs/research_documents/source_index.json",
            "items_path": "rows",
            "min_items_total": 2,
            "required_item_fields": ["title", "url", "date"],
            "require_completion_evidence": True,
            "mapping": {
                "artifact_ref": "outputs/research_documents/research_documents_zh.md",
                "key_fields": ["title"],
                "min_mapped_items": 2,
            },
        }
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        _write_json_file(
            workspace / "outputs/research_documents/source_index.json",
            {
                "completion_evidence": {"scope": "complete"},
                "rows": [
                    {"title": "Paper A", "url": "https://example.com/a", "date": "2026-01-01"},
                    {"title": "Paper B", "url": "https://example.com/b", "date": "2026-01-02"},
                ],
            },
        )
        draft = workspace / "outputs/research_documents/research_documents_zh.md"
        draft.write_text("# 翻译正文\n\n这里没有精确标题映射。", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(draft) in action["repair_targets"]


# LLM: JSON pointer fragments identify rows, not filesystem names.
# 函数用途: 验证 `source_index.json#1:field` 这类 finding location 会解析到真实 JSON 文件，不会拼出重复目录。
def test_delivery_closeout_strips_json_fragment_from_repair_target_locations():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        contract = _research_pdf_contract()
        validation_contract = contract["artifacts"][0]["validation_contract"]
        validation_contract["collection_contract"] = {
            "source_json_ref": "outputs/research_documents/source_index.json",
            "items_path": "rows",
            "min_items_total": 2,
            "required_item_fields": ["title", "url", "date", "translated"],
            "required_item_values": {"translated": True},
        }
        _write_valid_pdf(workspace / "outputs/research_documents/research_documents_zh.pdf")
        source = workspace / "outputs/research_documents/source_index.json"
        _write_json_file(
            source,
            {
                "rows": [
                    {"title": "Paper A", "url": "https://example.com/a", "date": "2026-01-01", "translated": True},
                    {"title": "Paper B", "url": "https://example.com/b", "date": "2026-01-02", "translated": False},
                ],
            },
        )

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(source) in action["repair_targets"]
        assert not any("source_index.json#1" in item for item in action["repair_targets"])
        assert not any("outputs/research_documents/outputs/research_documents" in item for item in action["repair_targets"])


# LLM: dotted API names are finding facts, not files to patch.
# 函数用途: 验证 `app.goBrowse` 这类 JS API finding value 不会被 closeout 拼成假 repair target。
def test_delivery_closeout_does_not_treat_dotted_api_findings_as_file_targets():
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
            '<!doctype html><html><head></head><body><button onclick="app.goBrowse()">Go</button><script src="app.js"></script></body></html>',
            encoding="utf-8",
        )
        (site / "app.js").write_text("const app = {};", encoding="utf-8")

        _, actions = _enriched_report(workspace, contract)

        action = actions["ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED"]
        assert str(site / "index.html") in action["repair_targets"]
        assert str(site / "app.js") in action["repair_targets"]
        assert not any(item.endswith("/app.goBrowse") for item in action["repair_targets"])


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
