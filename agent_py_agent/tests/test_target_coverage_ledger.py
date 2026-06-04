from __future__ import annotations


def test_target_coverage_ledger_reports_missing_items_without_blocking():
    from agent_py_agent.agent.agent_core.target_coverage_ledger import target_coverage_status

    status = target_coverage_status(
        {
            "scope_label": "今年以来每周",
            "enforcement": "advisory",
            "target_items": [
                {"target_id": "2026-W01", "label": "第1周"},
                {"target_id": "2026-W02", "label": "第2周"},
                {"target_id": "2026-W03", "label": "第3周"},
            ],
        },
        coverage_records=[
            {"target_id": "2026-W01", "status": "covered", "artifact_ref": "outputs/week1.xlsx"},
            {"target_id": "2026-W03", "status": "covered", "artifact_ref": "outputs/week3.xlsx"},
        ],
    )

    assert status["scope_label"] == "今年以来每周"
    assert status["enforcement"] == "advisory"
    assert status["expected_count"] == 3
    assert status["covered_count"] == 2
    assert status["missing_count"] == 1
    assert status["missing_items"] == [{"target_id": "2026-W02", "label": "第2周"}]
    assert status["should_block"] is False


def test_target_coverage_ledger_blocks_required_missing_read_targets(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("A", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"path": str(source_a)},
            }
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "source shards",
            "enforcement": "required",
            "target_items": [
                {"target_id": str(source_a), "label": "a"},
                {"target_id": str(source_b), "label": "b"},
            ],
        },
        coverage_records=records,
    )

    assert status["covered_count"] == 1
    assert status["missing_count"] == 1
    assert status["missing_items"] == [{"target_id": str(source_b), "label": "b"}]
    assert status["should_block"] is True
    assert status["recommended_next_action"] == "cover_missing_targets_before_submit"


def test_target_coverage_ledger_accepts_delivery_contract_items_alias(tmp_path):
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    source_a = tmp_path / "a.md"
    source_b = tmp_path / "b.md"
    source_a.write_text("A", encoding="utf-8")
    source_b.write_text("B", encoding="utf-8")
    records = collect_target_coverage_records(
        [
            {"tool": "read_file", "ok": True, "parameters": {"path": str(source_a)}},
            {"tool": "read_file", "ok": True, "parameters": {"path": str(source_b)}},
        ]
    )
    status = target_coverage_status(
        {
            "scope_label": "source shards",
            "enforcement": "required",
            "items": [
                {"target_id": str(source_a), "source_ref": str(source_a)},
                {"target_id": str(source_b), "source_ref": str(source_b)},
            ],
        },
        coverage_records=records,
    )

    assert status["expected_count"] == 2
    assert status["covered_count"] == 2
    assert status["missing_count"] == 0
    assert status["should_block"] is False


def test_target_coverage_ledger_collects_records_from_nested_runtime_payloads():
    from agent_py_agent.agent.agent_core.target_coverage_ledger import (
        collect_target_coverage_records,
        target_coverage_status,
    )

    records = collect_target_coverage_records(
        [
            {
                "result": {
                    "coverage_records": [
                        {"target_id": "api-a", "status": "covered", "source_ref": "tool://query-a"},
                    ]
                }
            },
            {
                "artifact_registry_refs": [
                    {"artifact_id": "artifact-b", "metadata": {"coverage_items": ["api-b"]}},
                ]
            },
        ]
    )
    status = target_coverage_status(
        {"target_items": [{"target_id": "api-a"}, {"target_id": "api-b"}]},
        coverage_records=records,
    )

    assert {item["target_id"] for item in records} == {"api-a", "api-b"}
    assert status["covered_count"] == 2
    assert status["missing_count"] == 0
