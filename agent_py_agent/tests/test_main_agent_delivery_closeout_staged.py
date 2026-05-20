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
    assert "STAGING_BUILDER_READY" not in actions


# LLM: Contract-driven recovery actions should expose builder readiness only after staged JSON is structurally usable.
# 函数用途: 验证 source_data.json 已有非空结构化数据时，closeout 才会生成 invoke_builder_tool 动作。
def test_delivery_closeout_adds_generic_staging_builder_action_for_ready_source():
    actions = _actions_for_source(_valid_rows_json())

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


# LLM: _write_source materializes the staged JSON checkpoint under the task workspace.
# 函数用途: 写 outputs/github_star_growth/source_data.json，让 closeout 读取真实文件状态。
def _write_source(workspace: Path, source_content: str) -> None:
    source = workspace / "outputs/github_star_growth/source_data.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(source_content, encoding="utf-8")


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
