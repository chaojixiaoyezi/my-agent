"""policy_checks 模块测试。

测试 policy_checks.py 中的纯策略函数：
_status_from_structured_output、_verification_from_runner_status、_runner_next_action 等。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentParsedOutput
from agent_py_agent.agent.subagents.policy_checks import (
    RunnerNextActionParams,
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


def test_status_from_structured_output_only_exact_done_completes():
    """只有当前结构化 DONE 状态能进入完成态。"""
    parsed = SubAgentParsedOutput(status="DONE", capability_requests=[], blocked_reason="")
    assert _status_from_structured_output(parsed) == "DONE"

    for status in ["COMPLETED", "COMPLETE", "SUCCESS"]:
        parsed = SubAgentParsedOutput(status=status, capability_requests=[], blocked_reason="")
        assert _status_from_structured_output(parsed) == "BLOCKED"


def test_status_from_structured_output_unknown_becomes_blocked():
    """未知状态不能被当作完成。"""
    parsed = SubAgentParsedOutput(status="UNKNOWN_STATUS", capability_requests=[], blocked_reason="")
    assert _status_from_structured_output(parsed) == "BLOCKED"


def test_status_from_structured_output_pending_capability_blocks():
    """测试模型只写 pending capability 状态时也不能进入验收。"""
    parsed = SubAgentParsedOutput(
        status="PENDING_CAPABILITY_REQUEST",
        capability_requests=[],
        blocked_reason="",
    )
    assert _status_from_structured_output(parsed) == "BLOCKED"


def test_status_from_structured_output_strips_whitespace():
    """测试状态值去除空白。"""
    parsed = SubAgentParsedOutput(status="  done  ", capability_requests=[], blocked_reason="")
    assert _status_from_structured_output(parsed) == "DONE"


# ── _verification_from_runner_status 测试 ─────────────────────────────────

def test_verification_pending_closeout():
    """测试待收口状态返回 VERIFIED。"""
    result = _verification_from_runner_status("DONE")
    assert result == "VERIFIED"


def test_verification_other_statuses():
    """测试其他状态返回 UNVERIFIED。"""
    for status in ["RUNNING", "FAILED", "BLOCKED"]:
        result = _verification_from_runner_status(status)
        assert result == "UNVERIFIED"


# ── _runner_next_action 测试 ───────────────────────────────────────────────

def test_runner_next_action_dry_run():
    """测试 dry_run 不建议动作。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=True,
            ok=True,
            status="RUNNING",
            capability_request_count=0,
        ),
    )
    assert result == ""


def test_runner_next_action_capability_request():
    """有能力请求时路由。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=True,
            status="RUNNING",
            capability_request_count=1,
        ),
    )
    assert result == "route_capability_request"


def test_runner_next_action_next_actions():
    """有 next_actions 时取第一个。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=True,
            status="RUNNING",
            capability_request_count=0,
            next_actions=["custom_action", "another"],
        ),
    )
    assert result == "custom_action"


def test_runner_next_action_pending_closeout():
    """DONE 时跑验收。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=True,
            status="DONE",
            capability_request_count=0,
        ),
    )
    assert result == ""


def test_runner_next_action_not_ok():
    """失败时检查失败原因。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=False,
            status="FAILED",
            capability_request_count=0,
        ),
    )
    assert result == "inspect_runner_failure"


def test_runner_next_action_no_suggestion():
    """没有任何线索时返回空。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=True,
            status="RUNNING",
            capability_request_count=0,
            next_actions=None,
        ),
    )
    assert result == ""


def test_runner_next_action_empty_next_actions():
    """空 next_actions 列表不触发自定义动作。"""
    result = _runner_next_action(
        params=RunnerNextActionParams(
            dry_run=False,
            ok=True,
            status="RUNNING",
            capability_request_count=0,
            next_actions=[],
        ),
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
    assert action == "reopen_for_evidence"


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


def test_action_for_issue_coordinator_heartbeat_stale():
    """失联 coordinator 动作为领导权恢复。"""
    issue = DueCheckIssue(
        run_id="root-1",
        severity="P1",
        kind="coordinator_heartbeat_stale",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "recover_coordinator_leadership"
    assert priority == 820
    assert new_status == ""


def test_action_for_issue_coordinator_needs_leadership_recovery():
    """死 coordinator 带子树时，动作优先级高于普通 takeover。"""
    issue = DueCheckIssue(
        run_id="root-timeout",
        severity="P0",
        kind="coordinator_needs_leadership_recovery",
        message="",
        suggested_action="recover_coordinator_leadership",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "recover_coordinator_leadership"
    assert priority == 930
    assert new_status == ""


def test_action_for_issue_parent_timeout_with_unfinished_children():
    """父超时但子任务未完成时，只生成子树恢复提示动作。"""
    issue = DueCheckIssue(
        run_id="root-timeout",
        severity="P1",
        kind="parent_timeout_with_unfinished_children",
        message="",
        suggested_action="",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "recover_child_after_parent_timeout"
    assert priority == 830
    assert new_status == ""


def test_action_for_issue_no_progress_fuse():
    """连续恢复无进展时生成停止自动重试动作。"""
    issue = DueCheckIssue(
        run_id="stuck-run",
        severity="P0",
        kind="no_progress_fuse",
        message="",
        suggested_action="stop_no_progress_and_escalate",
    )
    action, priority, new_status = _action_for_issue(issue)
    assert action == "stop_no_progress_and_escalate"
    assert priority == 990
    assert new_status == ""


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


def test_commands_for_action_recover_coordinator_leadership():
    """领导权恢复动作返回 recovery-tree 命令。"""
    commands = _commands_for_action("recover_coordinator_leadership", "root-1")
    assert any("subagents-recovery-tree root-1" in cmd for cmd in commands)


def test_commands_for_action_recover_child_after_parent_timeout():
    """父超时子任务恢复动作返回 recovery-tree 命令。"""
    commands = _commands_for_action("recover_child_after_parent_timeout", "root-timeout")
    assert any("subagents-recovery-tree root-timeout" in cmd for cmd in commands)


def test_commands_for_action_no_progress_fuse():
    """no-progress fuse 动作返回 run 和恢复树读取命令。"""
    commands = _commands_for_action("stop_no_progress_and_escalate", "stuck-run")
    assert any(command == "my-agent subagent stuck-run" for command in commands)
    assert any("subagents-recovery-tree stuck-run" in cmd for cmd in commands)


# ── _is_active 测试 ────────────────────────────────────────────────────────

def test_is_active_terminal():
    """终态返回 False。"""
    for status in ["DONE", "FAILED", "BLOCKED", "TIMEOUT", "CHANNEL_ERROR", "DONE", "TAKEN_OVER"]:
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
        params=RunnerNextActionParams(
            dry_run=True,
            ok=True,
            status="RUNNING",
            capability_request_count=5,
        ),
    )
    assert result == ""
