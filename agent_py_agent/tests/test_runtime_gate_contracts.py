from __future__ import annotations

from agent_py_agent.agent.contracts.gates.models import GateDecision, GateFinding


def test_gate_decision_serializes_recovery_envelope_with_chinese_message():
    decision = GateDecision.repair(
        "delivery_quality",
        [GateFinding("METRIC_WINDOW_MISSING", message="请补充时间窗口", evidence={"field": "stars_delta"})],
    )
    payload = decision.to_dict()

    recovery = payload["recovery"]
    action = recovery["actions"][0]
    assert recovery["status"] == "repair_required"
    assert recovery["can_auto_repair"] is True
    assert recovery["next_status"] == "REPAIRING"
    assert recovery["finding_codes"] == ["METRIC_WINDOW_MISSING"]
    assert action["recommended_action"] == "repair_structured_checkpoint_json"
    assert action["evidence"]["field"] == "stars_delta"
    assert "请" in recovery["message_zh"]


def test_gate_decision_exposes_action_and_operator_semantics():
    warning = GateDecision(
        "tool_guardrail",
        "ALLOW",
        True,
        (GateFinding("TOOL_GUARDRAIL_REPEAT_FAILURE_HINT", "P1", "换个查询方式", {"path": "logs/a.json"}),),
        "change_strategy",
        {"artifact_refs": ["reports/a.md"], "nested": {"path": "logs/a.json"}},
    )
    terminal = GateDecision(
        "tool_guardrail",
        "DENY",
        False,
        (GateFinding("TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"),),
        "change_strategy",
        {"block_task": True},
    )

    payload = warning.to_dict()

    assert payload["allow_action"] is True
    assert payload["block_task"] is False
    assert payload["severity"] == "engineering"
    assert payload["model_message"] == "换个查询方式"
    assert payload["operator_message"] == "tool_guardrail:ALLOW:TOOL_GUARDRAIL_REPEAT_FAILURE_HINT"
    assert payload["evidence_refs"] == ["reports/a.md", "logs/a.json"]
    assert terminal.to_dict()["block_task"] is True


def test_task_progress_open_items_are_repairable_not_terminal():
    decision = GateDecision.repair(
        "task_progress_closeout",
        [GateFinding("TASK_PROGRESS_OPEN_ITEMS", message="继续读取 fragment-015")],
    )
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False


def test_source_fact_identifier_repair_is_not_terminal():
    decision = GateDecision.repair(
        "source_fact_consistency",
        [GateFinding("SOURCE_CODE_IDENTIFIER_NOT_READ", message="补源码证据")],
    )

    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False
    assert recovery["can_auto_repair"] is True
    assert recovery["actions"][0]["retryable"] is True
    assert recovery["actions"][0]["recommended_action"] == "repair_evidence_refs"


def test_target_coverage_missing_is_repairable_not_terminal():
    decision = GateDecision.repair(
        "target_coverage",
        [GateFinding("TARGET_COVERAGE_MISSING", message="继续 read_file(start_line=501)")],
    )
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False
    assert recovery["can_auto_repair"] is True
    assert recovery["actions"][0]["recommended_action"] == "continue"


def test_gate_decision_recovery_envelope_marks_approval_as_user_input():
    decision = GateDecision.need_approval("tool_effect", evidence={"tool_name": "block_ip"})
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "needs_user_input"
    assert recovery["requires_user"] is True
    assert recovery["next_status"] == "WAITING_APPROVAL"
    assert recovery["actions"][0]["recommended_action"] == "request_user_input"
