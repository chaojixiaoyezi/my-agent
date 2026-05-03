"""Coverage tests for log_analysis/dispatch/work_orders/planning.py.

This module has helper functions and plan logic that the basic tests
don't cover - specifically the _get, _case_id, _merge_unique, _acceptance_checks
helpers and the dataclass serialization.
"""
from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.dispatch.work_orders import (
    NO_EVIDENCE_ISSUE,
    PARENT_FINAL_GATE,
    PLAN_NOT_READY_ISSUE,
    LogAnalysisWorkOrderPlan,
    SubagentWorkOrder,
    SubagentWorkOrderCreationResult,
)
from agent_py_agent.agent.log_analysis.dispatch.work_orders.planning import (
    _acceptance_checks,
    _case_id,
    _get,
    _merge_unique,
)


class TestGet:
    """Test _get helper function."""

    def test_get_from_mapping(self):
        """Test _get extracts value from mapping."""
        source = {"key": "value", "nested": {"a": 1}}

        assert _get(source, "key") == "value"
        assert _get(source, "nested") == {"a": 1}

    def test_get_with_default(self):
        """Test _get returns default for missing key."""
        source = {"key": "value"}

        assert _get(source, "missing") is None
        assert _get(source, "missing", "default") == "default"

    def test_get_from_object_attribute(self):
        """Test _get works with plain object having attributes."""
        class Obj:
            def __init__(self):
                self.name = "test"

        obj = Obj()
        assert _get(obj, "name") == "test"

    def test_get_falls_back_to_default(self):
        """Test _get falls back to default for missing attribute."""
        class Obj:
            pass

        obj = Obj()
        assert _get(obj, "missing", "fallback") == "fallback"

    def test_get_from_object_with_get_method(self):
        """Test _get works with object that has get method - getattr is used."""
        obj = MagicMock()
        obj.name = "stored_value"

        # MagicMock is not a Mapping, so getattr is used
        result = _get(obj, "name")
        # getattr returns the value we set
        assert result == "stored_value"


class TestCaseId:
    """Test _case_id helper function."""

    def test_case_id_from_dict_case_id(self):
        """Test extracts case_id from dict using case_id key."""
        case = {"case_id": "CASE-001", "title": "Test"}
        summary = {}

        assert _case_id(case, summary) == "CASE-001"

    def test_case_id_from_dict_id(self):
        """Test extracts case_id from dict using id key as fallback."""
        case = {"id": "CASE-ID-FROM-ID"}
        summary = {}

        assert _case_id(case, summary) == "CASE-ID-FROM-ID"

    def test_case_id_from_object(self):
        """Test extracts case_id from object."""
        case = MagicMock()
        case.case_id = "CASE-002"

        assert _case_id(case, {}) == "CASE-002"

    def test_case_id_falls_back_to_summary(self):
        """Test falls back to summary case_id."""
        case = {"title": "Test"}
        summary = {"case": {"case_id": "CASE-003"}}

        assert _case_id(case, summary) == "CASE-003"

    def test_case_id_falls_back_to_unknown(self):
        """Test falls back to unknown-case when no case_id found."""
        case = {"title": "Test"}
        summary = {}

        result = _case_id(case, summary)

        assert result == "unknown-case"


class TestMergeUnique:
    """Test _merge_unique helper function."""

    def test_merge_with_list_of_strings(self):
        """Test merging list of string evidence refs."""
        result = _merge_unique(["ref1", "ref2"], ["ref3"])

        # normalize_evidence_refs processes the input
        assert isinstance(result, list)

    def test_merge_with_none(self):
        """Test merging with None values."""
        result = _merge_unique(None, None)

        assert isinstance(result, list)

    def test_merge_empty_list(self):
        """Test merging empty list."""
        result = _merge_unique([])

        assert isinstance(result, list)


class TestAcceptanceChecks:
    """Test _acceptance_checks helper function."""

    def test_always_includes_default_checks(self):
        """Test always includes DEFAULT_ACCEPTANCE_CHECKS."""
        result = _acceptance_checks(None)

        # Should include defaults even with None
        assert isinstance(result, list)
        assert len(result) > 0

    def test_extracts_from_quality_contract(self):
        """Test extracts acceptance checks from quality contract."""
        contract = {
            "acceptance_checks": ["custom_check"],
        }

        result = _acceptance_checks(contract)

        assert isinstance(result, list)
        # Should contain both default and custom checks
        assert any("custom_check" in str(c) for c in result)


class TestSubagentWorkOrderDataclass:
    """Test SubagentWorkOrder dataclass."""

    def test_default_values(self):
        """Test default values are set correctly."""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="CASE-001",
            goal="Test goal",
        )

        assert order.mode == "manual"
        assert order.dry_run is True
        assert order.ready is False
        assert order.allowed_tools == []
        assert order.evidence_refs == []
        assert order.context == {}
        assert order.acceptance_checks == []
        assert order.cannot_self_accept is True
        assert order.parent_final_gate == PARENT_FINAL_GATE
        assert order.issues == []
        assert order.risks == []

    def test_to_dict(self):
        """Test to_dict serialization."""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="CASE-001",
            goal="Test goal",
            dry_run=False,
            ready=True,
        )

        d = order.to_dict()

        assert d["role"] == "analyst"
        assert d["case_id"] == "CASE-001"
        assert d["goal"] == "Test goal"
        assert d["dry_run"] is False
        assert d["ready"] is True

    def test_with_custom_tools(self):
        """Test work order with custom tools."""
        order = SubagentWorkOrder(
            role="reviewer",
            case_id="CASE-002",
            goal="Review report",
            allowed_tools=["evidence_read", "search"],
        )

        assert order.allowed_tools == ["evidence_read", "search"]

    def test_with_issues_and_risks(self):
        """Test work order with issues and risks."""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="CASE-003",
            goal="Analyze",
            issues=["missing_data"],
            risks=["incomplete_coverage"],
        )

        assert order.issues == ["missing_data"]
        assert order.risks == ["incomplete_coverage"]


class TestLogAnalysisWorkOrderPlanDataclass:
    """Test LogAnalysisWorkOrderPlan dataclass."""

    def test_default_values(self):
        """Test default values are set correctly."""
        plan = LogAnalysisWorkOrderPlan(
            case_id="CASE-001",
            ready=True,
        )

        assert plan.dry_run is True
        assert plan.mode == "manual"
        assert plan.work_orders == []
        assert plan.issues == []
        assert plan.risks == []

    def test_to_dict_with_work_orders(self):
        """Test to_dict serializes nested work orders."""
        order = SubagentWorkOrder(
            role="analyst",
            case_id="CASE-001",
            goal="Test",
        )
        plan = LogAnalysisWorkOrderPlan(
            case_id="CASE-001",
            ready=True,
            work_orders=[order],
        )

        d = plan.to_dict()

        assert len(d["work_orders"]) == 1
        assert d["work_orders"][0]["role"] == "analyst"

    def test_to_dict_preserves_case_id(self):
        """Test to_dict preserves case_id."""
        plan = LogAnalysisWorkOrderPlan(
            case_id="CASE-SPECIAL",
            ready=True,
        )

        d = plan.to_dict()

        assert d["case_id"] == "CASE-SPECIAL"

    def test_with_issues_and_risks(self):
        """Test plan with issues and risks."""
        plan = LogAnalysisWorkOrderPlan(
            case_id="CASE-004",
            ready=False,
            issues=["no_evidence"],
            risks=["limited_data"],
        )

        assert plan.ready is False
        assert plan.issues == ["no_evidence"]
        assert plan.risks == ["limited_data"]


class TestSubagentWorkOrderCreationResult:
    """Test SubagentWorkOrderCreationResult dataclass."""

    def test_dry_run_mode(self):
        """Test dry_run creation result."""
        result = SubagentWorkOrderCreationResult(
            case_id="CASE-001",
            ready=True,
            apply=False,
            dry_run=True,
            mode="dry_run",
        )

        assert result.dry_run is True
        assert result.apply is False
        assert result.task_ids == []
        assert result.created == []

    def test_apply_mode(self):
        """Test apply mode creation result."""
        result = SubagentWorkOrderCreationResult(
            case_id="CASE-001",
            ready=True,
            apply=True,
            dry_run=False,
            mode="apply",
            created=[{"task_id": "task-1", "role": "analyst"}],
            task_ids=["task-1"],
        )

        assert result.dry_run is False
        assert result.apply is True
        assert len(result.task_ids) == 1

    def test_to_dict_creation_result(self):
        """Test to_dict on creation result."""
        result = SubagentWorkOrderCreationResult(
            case_id="CASE-001",
            ready=True,
            apply=True,
            dry_run=False,
            mode="apply",
            task_ids=["task-1"],
        )

        d = result.to_dict()

        assert d["case_id"] == "CASE-001"
        assert d["task_ids"] == ["task-1"]


class TestConstants:
    """Test module constants."""

    def test_no_evidence_issue_message(self):
        """Test NO_EVIDENCE_ISSUE is defined."""
        assert NO_EVIDENCE_ISSUE is not None
        assert "evidence" in NO_EVIDENCE_ISSUE.lower()

    def test_parent_final_gate_constant(self):
        """Test PARENT_FINAL_GATE is defined."""
        assert PARENT_FINAL_GATE is not None
        assert PARENT_FINAL_GATE == "parent_session_final_approval_required"

    def test_plan_not_ready_issue_constant(self):
        """Test PLAN_NOT_READY_ISSUE is defined."""
        assert PLAN_NOT_READY_ISSUE is not None
        assert "not ready" in PLAN_NOT_READY_ISSUE.lower()