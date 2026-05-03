"""policy_checks 模块测试。

测试 policy_checks.py 中的纯策略函数：
_status_from_structured_output、_verification_from_runner_status、_runner_next_action 等。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.policy_checks import (
    _action_for_issue,
    _commands_for_action,
    _default_forbidden_write_roots,
    _is_active,
    _issue_weight,
    _risk_weight,
    _runner_next_action,
    _severity_weight,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentParsedOutput
from agent_py_agent.agent.subagents.reports import DueCheckIssue


# ── _status_from_structured_output 测试 ───────────────────────────────────

def test_status_from_structured_output_blocked_by_capability():
    """测试有能力请求时变成 BLOCKED。"""
    parsed = SubAgentParsedOutput(
        status="DONE",
        capability_requests=[CapabilityRequest(
            id="req1",
            from_run_id="r1",
            needed_capability="test",
            problem="",
            expected_output="",
        )],
        blocked_reason="",
    )
    assert _status_from_structured_output(parsed) == "BLOCKED"


def test_status_from_structured_output_blocked_by_reason():
    """测试有 blocked_reason 时变成 BLOCKED。"""
    parsed = SubAgentParsedOutput(
        status="RUNNING",
        capability_requests=[],
        blocked_reason="等待资源",
    )
    assert _status_from_structured_output(parsed) == "BLOCKED"


def test_status_from_structured_output_preserves_fail_states():
    """测试失败类状态保持不变。"""
    for status in ["BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"]:
        parsed = SubAgentParsedOutput(status=status, capability_requests=[], blocked_reason="")
        assert _status_from_structured_output(parsed) == status


def test_status_from_structured_output_done_becomes_awaiting():
    """测试 DONE 类状态变成 AWAITING_ACCEPTANCE。"""
    for status in ["DONE", "COMPLETED", "COMPLETE", "SUCCESS"]:
        parsed = SubAgentParsedOutput(status=status, capability_requests=[], blocked_reason="")
        assert _status_from_structured_output(parsed) == "AWAITING_ACCEPTANCE"


def test_status_from_structured_output_unknown_becomes_awaiting():
    """测试未知状态变成 AWAITING_ACCEPTANCE。"""
    parsed = SubAgentParsedOutput(status="UNKNOWN_STATUS", capability_requests=[], blocked_reason="")
    assert _status_from_structured_output(parsed) == "AWAITING_ACCEPTANCE"


def test_status_from_structured_output_strips_whitespace():
    """测试状态值去除空白。"""
    parsed = SubAgentParsedOutput(status="  done  ", capability_requests=[], blocked_reason="")
    assert _status_from_structured_output(parsed) == "AWAITING_ACCEPTANCE"


# ── _verification_from_runner_status 测试 ─────────────────────────────────

def test_verification_awaiting_acceptance():
    """测试待验收状态返回 NEEDS_ACCEPTANCE。"""
    result = _verification_from_runner_status("AWAITING_ACCEPTANCE")
    assert result == "NEEDS_ACCEPTANCE"


def test_verification_other_statuses():
    """测试其他状态返回 UNVERIFIED。"""
    for status in ["RUNNING", "DONE", "FAILED", "BLOCKED"]:
        result = _verification_from_runner_status(status)
        assert result == "UNVERIFIED"


# ── _runner_next_action 测试 ───────────────────────────────────────────────

def test_runner_next_action_dry_run():
    """测试 dry_run 不建议动作。"""
    result = _runner_next_action(
        dry_run=True,
        ok=True,
        status="RUNNING",
        capability_request_count=0,
    )
    assert result == ""


def test_runner_next_action_capability_request():
    """有能力请求时路由。"""
    result = _runner_next_action(
        dry_run=False,
        ok=True,
        status="RUNNING",
        capability_request_count=1,
    )
    assert result == "route_capability_request"


def test_runner_next_action_next_actions():
    """有 next_actions 时取第一个。"""
    result = _runner_next_action(
        dry_run=False,
        ok=True,
        status="RUNNING",
        capability_request_count=0,
        next_actions=["custom_action", "another"],
    )
    assert result == "custom_action"


def test_runner_next_action_awaiting_acceptance():
    """AWAITING_ACCEPTANCE 时跑验收。"""
    result = _runner_next_action(
        dry_run=False,
        ok=True,
        status="AWAITING_ACCEPTANCE",
        capability_request_count=0,
    )
    assert result == "run_acceptance"


def test_runner_next_action_not_ok():
    """失败时检查失败原因。"""
    result = _runner_next_action(
        dry_run=False,
        ok=False,
        status="FAILED",
        capability_request_count=0,
    )
    assert result == "inspect_runner_failure"


def test_runner_next_action_no_suggestion():
    """没有任何线索时返回空。"""
    result = _runner_next_action(
        dry_run=False,
        ok=True,
        status="RUNNING",
        capability_request_count=0,
        next_actions=None,
    )
    assert result == ""


def test_runner_next_action_empty_next_actions():
    """空 next_actions 列表不触发自定义动作。"""
    result = _runner_next_action(
        dry_run=False,
        ok=True,
        status="RUNNING",
        capability_request_count=0,
        next_actions=[],
    )
    assert result == ""


# ── _risk_weight 测试 ──────────────────────────────────────────────────────

def test_risk_weight_failed():
    """测试 failed 最高权重。"""
    assert _risk_weight(["failed"]) == 100


def test_risk_weight_timeout():
    """测试 timeout 权重。"""
    assert _risk_weight(["timeout"]) == 95


def test_risk_weight_taken_over():
    """测试 taken_over 低权重。"""
    assert _risk_weight(["taken_over"]) == 30


def test_risk_weight_multiple():
    """测试多标志取最大值。"""
    assert _risk_weight(["open_capability_request", "failed"]) == 100


def test_risk_weight_unknown():
    """测试未知标志返回默认权重。"""
    assert _risk_weight(["unknown_flag"]) == 1


def test_risk_weight_empty():
    """测试空列表返回 0。"""
    assert _risk_weight([]) == 0


# ── _severity_weight 测试 ─────────────────────────────────────────────────

def test_severity_weight_p0():
    """P0 权重最高。"""
    assert _severity_weight("P0") == 1000


def test_severity_weight_p1():
    """P1 权重。"""
    assert _severity_weight("P1") == 500


def test_severity_weight_p2():
    """P2 权重最低。"""
    assert _severity_weight("P2") == 100


def test_severity_weight_unknown():
    """未知返回 0。"""
    assert _severity_weight("P5") == 0


# ── _issue_weight 测试 ─────────────────────────────────────────────────────

def test_issue_weight_missing_work_order():
    """工单缺失权重高。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="missing_work_order_files",
        message="",
        suggested_action="",
    )
    weight = _issue_weight(issue)
    assert weight >= 1090  # P0(1000) + missing_work_order(90)


def test_issue_weight_open_capability_gap():
    """能力缺口权重较低。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P2",
        kind="open_capability_gap",
        message="",
        suggested_action="",
    )
    weight = _issue_weight(issue)
    assert weight < 200  # P2(100) + gap(20)


def test_issue_weight_fake_done_risk():
    """伪完成风险权重高。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="fake_done_risk",
        message="",
        suggested_action="",
    )
    weight = _issue_weight(issue)
    assert weight >= 1085  # P0(1000) + fake_done(85)


# ── _action_for_issue 测试 ─────────────────────────────────────────────────

def test_action_for_issue_channel_broken():
    """通道损坏动作为 probe_or_repair。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="channel_broken",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "probe_or_repair_channel"
    assert priority == 980
    assert new_status == "CHANNEL_ERROR"


def test_action_for_issue_fake_done_risk():
    """伪完成风险动作为重新打开。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="fake_done_risk",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "reopen_for_evidence"
    assert new_status == "BLOCKED"


def test_action_for_issue_unverified_done():
    """未验证完成动作为运行验收。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P1",
        kind="unverified_done",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "run_acceptance"


def test_action_for_issue_missing_work_order():
    """工单缺失动作为 repair_work_order。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="missing_work_order_files",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "repair_work_order"
    assert new_status == "BLOCKED"


def test_action_for_issue_unknown_kind():
    """未知问题类型返回建议动作。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P1",
        kind="unknown_kind",
        message="",
        suggested_action="custom_action",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "custom_action"


def test_action_for_issue_channel_degraded():
    """通道降级动作为检查探针。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P1",
        kind="channel_degraded",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "inspect_channel_probe"


# ── _commands_for_action 测试 ──────────────────────────────────────────────

def test_commands_for_action_takeover():
    """接管动作返回探针和 subagent 命令。"""
    commands = _commands_for_action("takeover_or_reassign", "run-99")
    assert len(commands) >= 2
    assert any("run-99" in cmd for cmd in commands)


def test_commands_for_action_reopen():
    """重新打开动作用 subagent 命令。"""
    commands = _commands_for_action("reopen_for_evidence", "run-x")
    assert "run-x" in commands[0]


def test_commands_for_action_unknown():
    """未知动作用通用 subagent 命令。"""
    commands = _commands_for_action("unknown", "run-y")
    assert len(commands) >= 1
    assert "run-y" in commands[0]


# ── _is_active 测试 ────────────────────────────────────────────────────────

def test_is_active_terminal():
    """终态返回 False。"""
    for status in ["DONE", "FAILED", "BLOCKED", "TIMEOUT", "CHANNEL_ERROR", "AWAITING_ACCEPTANCE", "TAKEN_OVER"]:
        assert _is_active(status) is False


def test_is_active_running():
    """运行状态返回 True。"""
    assert _is_active("RUNNING") is True
    assert _is_active("PENDING") is True


# ── _default_forbidden_write_roots 测试 ────────────────────────────────────

def test_default_forbidden_write_roots_not_empty():
    """非空列表。"""
    roots = _default_forbidden_write_roots()
    assert len(roots) >= 4


def test_default_forbidden_write_roots_contains_home():
    """包含主目录。"""
    from pathlib import Path
    roots = _default_forbidden_write_roots()
    assert any(str(Path.home()) in r for r in roots)


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_status_from_structured_output_capability_priority():
    """能力请求优先于 blocked_reason。"""
    parsed = SubAgentParsedOutput(
        status="DONE",
        capability_requests=[CapabilityRequest(
            id="req1",
            from_run_id="r1",
            needed_capability="test",
            problem="",
            expected_output="",
        )],
        blocked_reason="some reason",
    )
    assert _status_from_structured_output(parsed) == "BLOCKED"


def test_runner_next_action_dry_run_with_capability():
    """dry_run 时即使有能力请求也返回空。"""
    result = _runner_next_action(
        dry_run=True,
        ok=True,
        status="RUNNING",
        capability_request_count=5,
    )
    assert result == ""