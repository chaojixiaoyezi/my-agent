from __future__ import annotations

import json
from pathlib import Path


def test_real_run_review_clusters_failed_acceptance_reports(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_run_review import review_real_run_tree

    _write_json(
        tmp_path / "main-agent-stage3-github-retry15-20260521" / "main_agent_task_execution" / "tasks" / "github" / "acceptance_report.json",
        {
            "ok": False,
            "findings": [
                {"code": "XLSX_MISSING_REQUIRED_COLUMNS"},
                {"code": "EVIDENCE_REQUIRED_FIELD_MISSING"},
            ],
        },
    )
    _write_text(
        tmp_path / "main-agent-stage3-github-retry15-20260521" / "main_agent_task_execution" / "tasks" / "github" / "stdout.txt",
        "[DELIVERY_REQUIRED_REPAIR_BLOCKED] repair blocked",
    )

    review = review_real_run_tree(tmp_path, run_glob="*20260521*")

    assert review.summary == {"failed": 1, "passed": 0, "total": 1, "unknown": 0}
    assert review.records[0].root_cause_tags == (
        "structured_columns_missing",
        "evidence_claims_missing",
        "delivery_repair_blocked",
    )
    assert review.records[0].priority == "P1"
    assert review.clusters[0].tag == "delivery_repair_blocked"
    assert review.clusters[0].count == 1
    assert review.clusters[0].run_ids == ("main-agent-stage3-github-retry15-20260521",)


def test_real_run_review_preserves_success_and_static_site_failures(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_run_review import review_real_run_tree

    _write_json(tmp_path / "main-agent-stage4-shopping-retry3-20260521" / "real_e2e_report.json", {"ok": True})
    _write_json(
        tmp_path / "main-agent-stage4-shopping-retry2-20260521" / "main_agent_task_execution" / "tasks" / "shopping" / "acceptance_report.json",
        {"ok": False, "findings": [{"code": "STATIC_SITE_MISSING_DOM_ID_HITS"}]},
    )

    review = review_real_run_tree(tmp_path, run_glob="*20260521*")
    failed = next(record for record in review.records if record.final_status == "FAILED")
    passed = next(record for record in review.records if record.final_status == "PASSED")

    assert review.summary == {"failed": 1, "passed": 1, "total": 2, "unknown": 0}
    assert failed.root_cause_tags == ("static_site_contract_failed",)
    assert failed.first_failure_code == "STATIC_SITE_MISSING_DOM_ID_HITS"
    assert passed.root_cause_tags == ()
    assert passed.first_failure_code == ""


def test_real_run_review_rendering_is_refs_first(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_run_review import (
        render_real_run_review_markdown,
        review_real_run_tree,
    )

    _write_json(
        tmp_path / "main-agent-stage5-research-pdf-20260521" / "main_agent_task_execution" / "tasks" / "research" / "acceptance_report.json",
        {"ok": False, "findings": [{"code": "ARTIFACT_MISSING"}]},
    )

    markdown = render_real_run_review_markdown(review_real_run_tree(tmp_path, run_glob="*20260521*"))

    assert "main-agent-stage5-research-pdf-20260521" in markdown
    assert "ARTIFACT_MISSING" in markdown
    assert "artifact_missing" in markdown
    assert "acceptance_report.json" in markdown
    assert "自然语言" not in markdown


def test_real_run_review_ignores_non_failure_runtime_markers(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_run_review import review_real_run_tree

    _write_text(
        tmp_path / "parallel-main-agents-20260521-1" / "stdout.txt",
        "[TOOL_CALL] read_file\n[FILE_WRITE_SESSION_APPEND] chunk ok",
    )

    review = review_real_run_tree(tmp_path, run_glob="*20260521*")

    assert review.records[0].first_failure_code == ""
    assert review.records[0].last_surface_code == ""
    assert review.records[0].evidence_refs == ()


def test_real_run_review_maps_generic_evidence_and_path_codes(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.real_run_review import review_real_run_tree

    _write_json(
        tmp_path / "main-agent-stage2-furniture-20260521" / "report.json",
        {
            "ok": False,
            "findings": [
                {"code": "ARTIFACT_PATH_OUTSIDE_WORKSPACE"},
                {"code": "EVIDENCE_CLAIM_UNSOURCED"},
            ],
        },
    )

    record = review_real_run_tree(tmp_path, run_glob="*20260521*").records[0]

    assert record.root_cause_tags == ("artifact_path_mismatch", "evidence_claims_missing")
    assert record.priority == "P1"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
