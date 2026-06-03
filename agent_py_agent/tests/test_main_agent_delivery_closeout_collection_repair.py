from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    xlsx_delivery_contract,
)


def test_collection_checkpoint_quality_prefers_api_collection_writer() -> None:
    contract = xlsx_delivery_contract()
    validation = contract["artifacts"][0]["validation_contract"]
    validation["collection_contract"] = {
        "source_json_ref": "outputs/table_report/source_data.json",
        "groups_path": "sheets",
        "items_path": "rows",
        "min_groups": 2,
        "min_items_per_group": 10,
        "required_item_fields": ["记录名", "地址", "指标值", "中文说明", "说明依据"],
        "require_completion_evidence": True,
    }

    actions = _actions_for_source(_bad_collection_source(), contract)

    action = actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
    assert action["writer_tool"] == "write_file"
    assert action["write_tools"] == ["write_file"]
    assert action["collection_contract"]["source_json_ref"] == "outputs/table_report/source_data.json"


def test_collection_too_few_items_routes_to_api_collection_writer() -> None:
    contract = xlsx_delivery_contract()
    validation = contract["artifacts"][0]["validation_contract"]
    validation["collection_contract"] = {
        "source_json_ref": "outputs/table_report/source_data.json",
        "groups_path": "sheets",
        "items_path": "rows",
        "min_items_total": 3,
        "required_item_fields": ["记录名", "地址", "指标值"],
        "require_completion_evidence": True,
    }

    actions = _actions_for_source(_small_valid_collection_source(), contract)

    action = actions["COLLECTION_TOO_FEW_ITEMS"]
    assert action["recommended_action"] == "repair_structured_checkpoint_json"
    assert action["checkpoint_ref"] == "outputs/table_report/source_data.json"
    assert action["writer_tool"] == "write_file"
    assert action["write_tools"] == ["write_file"]
    assert action["collection_contract"]["min_items_total"] == 3


def test_collection_checkpoint_quality_prefers_structured_writer_for_generated_rows() -> None:
    contract = xlsx_delivery_contract()
    validation = contract["artifacts"][0]["validation_contract"]
    validation["collection_contract"] = {
        "source_json_ref": "outputs/table_report/source_data.json",
        "items_path": "rows",
        "required_item_fields": ["记录名", "地址", "指标值"],
        "require_completion_evidence": True,
    }
    validation["staging_contract"]["checkpoint_shape_hints"] = {
        "outputs/table_report/source_data.json": json.dumps(
            {
                "data": {"completion_evidence": {"scope": "synthetic_dataset", "row_count": 1000}},
                "generated_rows": {
                    "count": 1000,
                    "columns": ["记录名", "地址", "指标值"],
                    "fields": {
                        "记录名": {"format": "repo-{index:04d}", "start": 1},
                        "地址": {"format": "https://example.com/repo-{index:04d}", "start": 1},
                        "指标值": {"number": {"start": 100, "step": 7}},
                    },
                    "sheets": {"count": 3, "prefix": "数据"},
                },
            },
            ensure_ascii=False,
        )
    }

    actions = _actions_for_source(_bad_collection_source(), contract)

    action = actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
    assert action["writer_tool"] == "write_file"
    assert action["write_tools"] == ["write_file"]


def _actions_for_source(source_content: str, contract: dict[str, object]) -> dict[str, dict[str, object]]:
    from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
        DeliveryContractValidationRequest,
        DeliveryProgressContext,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/table_report/source_data.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source_content, encoding="utf-8")
        report = _validate_contract_artifacts(
            DeliveryContractValidationRequest(
                contract=contract,
                artifacts=contract["artifacts"],
                workspace_root=workspace,
                params=RunParams(delivery_contract=contract, save=False),
            )
        )
        enriched = _enrich_delivery_progress(report, {}, DeliveryProgressContext(workspace, contract))
    return {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}


def _bad_collection_source() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["记录名", "地址", "指标值"],
                    "rows": [{"记录名": "demo", "地址": "https://example.com", "指标值": 10}],
                }
            ]
        },
        ensure_ascii=False,
    )


def _small_valid_collection_source() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["记录名", "地址", "指标值"],
                    "rows": [
                        {"记录名": "demo", "地址": "https://example.com", "指标值": 10},
                        {"记录名": "demo2", "地址": "https://example.com/2", "指标值": 8},
                    ],
                }
            ],
            "completion_evidence": {"method": "fixture", "scope": "unit"},
        },
        ensure_ascii=False,
    )
