from __future__ import annotations


def test_materialized_delivery_contract_accepts_generic_artifacts_without_paths():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "schema_version": "delivery_requirement_materializer.v1",
            "artifacts": [
                {
                    "artifact_id": "final_workbook",
                    "kind": "xlsx",
                    "required": True,
                    "allowed_output_roots": ["outputs"],
                }
            ],
            "delivery_quality_contract": {
                "metric_contracts": [
                    {
                        "field": "star_delta",
                        "expected_kind": "period_delta",
                        "required_window": True,
                    }
                ]
            },
        }
    )

    artifact = contract["artifacts"][0]
    assert contract["schema_version"] == "delivery_contract.v1"
    assert artifact["artifact_id"] == "final_workbook"
    assert artifact["kind"] == "xlsx"
    assert artifact["allowed_output_roots"] == ["outputs"]
    assert "preferred_path" not in artifact
    assert contract["delivery_quality_contract"]["metric_contracts"][0]["field"] == "star_delta"


def test_materialized_delivery_contract_rejects_unbounded_absolute_artifact_path(tmp_path):
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        materialized_delivery_contract,
    )

    contract = materialized_delivery_contract(
        {
            "artifacts": [
                {
                    "artifact_id": "bad",
                    "kind": "pdf",
                    "preferred_path": "/etc/passwd",
                }
            ]
        },
        workspace_root=tmp_path,
    )

    finding_codes = [item["code"] for item in contract["_preflight_findings"]]
    assert "DELIVERY_MATERIALIZER_ARTIFACT_PATH_OUTSIDE_WORKSPACE" in finding_codes
    assert contract["artifacts"] == []


def test_materializer_prompt_asks_for_structured_contract_not_task_template():
    from agent_py_agent.agent.agent_core.delivery_requirement_materializer import (
        build_delivery_requirement_materializer_prompt,
    )

    prompt = build_delivery_requirement_materializer_prompt("帮我生成一个文件，放到输出目录")

    assert "delivery_contract.v1" in prompt
    assert "不要写具体执行步骤模板" in prompt
    assert "GitHub" not in prompt
    assert "论文" not in prompt

