from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.tests.support.main_agent_delivery_closeout_fixtures import (
    xlsx_delivery_contract,
)


# LLM: Collection checkpoint quality failures route to collection tools, not manual JSON patching.
# 函数用途: 验证带 collection_contract 的 source_data 质量失败时，恢复动作优先要求 api_json_collection。
def test_collection_checkpoint_quality_prefers_api_collection_writer() -> None:
    contract = xlsx_delivery_contract()
    validation = contract["artifacts"][0]["validation_contract"]
    validation["collection_contract"] = {
        "source_json_ref": "outputs/github_star_growth/source_data.json",
        "groups_path": "sheets",
        "items_path": "rows",
        "min_groups": 2,
        "min_items_per_group": 10,
        "required_item_fields": ["项目名", "地址", "上升 star 数", "中文解释", "推荐理由"],
        "require_completion_evidence": True,
    }

    actions = _actions_for_source(_bad_collection_source(), contract)

    action = actions["STAGED_JSON_REQUIRED_COLUMNS_MISSING"]
    assert action["writer_tool"] == "api_json_collection"
    assert action["write_tools"] == ["api_json_collection", "write_structured_json"]
    assert action["collection_contract"]["source_json_ref"] == "outputs/github_star_growth/source_data.json"


def _actions_for_source(source_content: str, contract: dict[str, object]) -> dict[str, dict[str, object]]:
    from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
        DeliveryContractValidationRequest,
        _enrich_delivery_progress,
        _validate_contract_artifacts,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td).resolve()
        source = workspace / "outputs/github_star_growth/source_data.json"
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
        enriched = _enrich_delivery_progress(report, {}, workspace, contract=contract)
    return {item["code"]: item for item in enriched["delivery_progress"]["recovery_actions"]}


def _bad_collection_source() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "name": "week-1",
                    "columns": ["项目名", "地址", "上升 star 数"],
                    "rows": [{"项目名": "demo", "地址": "https://example.com", "上升 star 数": 10}],
                }
            ]
        },
        ensure_ascii=False,
    )
