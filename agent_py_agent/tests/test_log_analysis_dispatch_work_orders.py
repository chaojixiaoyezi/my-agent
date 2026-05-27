"""Work order dispatch tests for log analysis.
日志分析工单派发测试。"""

from __future__ import annotations

from agent_py_agent.agent.log_analysis.dispatch import (
    create_subagent_tasks_from_work_order_plan,
    plan_case_subagent_work_orders,
)
from agent_py_agent.agent.subagent import SubAgentManager


def _case_fixture() -> dict:
    return {
        "case_id": "case-001",
        "title": "WAF to EDR weak-signal chain",
        "status": "OPEN",
        "priority": "P2",
        "risk_score": 0.72,
        "entity_refs": ["ip:198.51.100.1", "host:web-01"],
        "evidence_refs": [
            {"id": "ev-waf-1", "summary": "WAF hit", "rows": [{"raw": "do not include"}]},
            {"id": "ev-edr-1", "summary": "EDR process anomaly"},
        ],
        "route_draft": {
            "entry_candidates": ["waf_uri:/login"],
            "timeline": ["2026-04-30T01:00Z WAF hit", "2026-04-30T01:04Z EDR alert"],
            "gaps": ["Need VPN account context"],
            "next_queries": ["security_hunt_ip ip=198.51.100.1"],
        },
        "raw_events": [{"payload": "RAW_MARKER_SHOULD_NOT_APPEAR"}],
        "transcript": "TRANSCRIPT_MARKER_SHOULD_NOT_APPEAR" * 100,
    }


def test_work_order_plan_builds_manual_dry_run_analyst_and_reviewer_orders():
    """LLM: Tests that work order plan builds dry-run analyst and reviewer orders with correct tool sets."""
    plan = plan_case_subagent_work_orders(
        _case_fixture(),
        quality_contract={"acceptance_checks": ["Reviewer must enforce closeout."]},
    )
    payload = plan.to_dict()

    assert plan.ready
    assert plan.dry_run
    assert plan.mode == "manual"
    assert [order.role for order in plan.work_orders] == ["analyst", "reviewer"]
    assert payload["work_orders"][0]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]
    assert payload["work_orders"][1]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]

    analyst = payload["work_orders"][0]
    reviewer = payload["work_orders"][1]
    assert analyst["allowed_tools"] == [
        "security_query",
        "security_hunt_ip",
        "security_trace_case",
        "evidence_read",
    ]
    assert reviewer["allowed_tools"] == ["evidence_read"]
    assert reviewer["context"]["expected_input"] == "AnalystReport from analyst work order"
    assert "Reviewer must enforce closeout." in reviewer["acceptance_checks"]
    assert not plan.issues


def test_work_order_plan_without_evidence_refs_is_not_ready_and_reports_risk():
    """LLM: Tests that a work order plan without evidence_refs is not ready and reports risk."""
    case = {
        "case_id": "case-no-evidence",
        "title": "Suspicious signal without retained evidence",
        "route_draft": {"gaps": ["Need source query evidence"]},
    }

    plan = plan_case_subagent_work_orders(case)
    payload = plan.to_dict()

    assert not plan.ready
    assert plan.dry_run
    assert "no evidence_refs" in plan.issues[0]
    assert "unsupported analysis" in plan.risks[0]
    assert all(not order["ready"] for order in payload["work_orders"])
    assert all(order["evidence_refs"] == [] for order in payload["work_orders"])
    assert all(order["issues"] == plan.issues for order in payload["work_orders"])


def test_work_order_plan_apply_creates_analyst_and_reviewer_subagent_tasks(tmp_path):
    """LLM: Tests that applying a work order plan creates analyst and reviewer subagent tasks with proper fields."""
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(
        _case_fixture(),
        quality_contract={"acceptance_checks": ["Reviewer must enforce closeout."]},
    )

    result = create_subagent_tasks_from_work_order_plan(
        subagents,
        plan,
        apply=True,
        parent_id="parent-run",
        root_id="root-run",
        final_owner="parent",
    )
    tasks = [subagents.load(task_id) for task_id in result.task_ids]

    assert result.apply
    assert not result.dry_run
    assert result.mode == "apply"
    assert len(result.created) == 2
    assert [task.role for task in tasks] == ["analyst", "reviewer"]
    assert [task.agent_name for task in tasks] == ["log-analyst", "log-reviewer"]
    assert tasks[0].allowed_tools == [
        "security_query",
        "security_hunt_ip",
        "security_trace_case",
        "evidence_read",
    ]
    assert tasks[1].allowed_tools == ["evidence_read"]
    for task in tasks:
        assert task.status == "PLANNING"
        assert task.verification_status == "UNVERIFIED"
        assert task.runner_attempts == 0
        assert task.parent_id == "parent-run"
        assert task.root_id == "root-run"
        assert task.final_owner == "parent"
        assert "ev-waf-1" in task.quality_contract.evidence_required
        assert task.context_packs[0]["case_id"] == "case-001"
        assert task.context_packs[0]["evidence_refs"] == ["ev-waf-1", "ev-edr-1"]


def test_work_order_plan_dry_run_does_not_create_subagent_tasks(tmp_path):
    """LLM: Tests that a dry-run work order plan does not create any subagent tasks."""
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(_case_fixture())

    result = create_subagent_tasks_from_work_order_plan(subagents, plan)

    assert result.dry_run
    assert not result.apply
    assert result.mode == "dry_run"
    assert result.created == []
    assert result.task_ids == []
    assert list((tmp_path / "subagents").glob("*/task.json")) == []


def test_work_order_plan_apply_without_evidence_refuses_and_creates_no_tasks(tmp_path):
    """LLM: Tests that applying a plan without evidence refs refuses and creates no tasks."""
    subagents = SubAgentManager(tmp_path / "subagents")
    plan = plan_case_subagent_work_orders(
        {
            "case_id": "case-no-evidence",
            "title": "Suspicious signal without retained evidence",
        }
    )

    result = create_subagent_tasks_from_work_order_plan(subagents, plan, apply=True)

    assert not result.ready
    assert result.apply
    assert not result.dry_run
    assert result.created == []
    assert result.task_ids == []
    assert "not ready" in result.issues[-1]
    assert list((tmp_path / "subagents").glob("*/task.json")) == []
