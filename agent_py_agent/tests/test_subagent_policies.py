"""subagent policies 模块测试。

测试 filter_board_items、_filter_action_plan_items、_capability_request_query、
_select_capability_hits 等纯策略函数。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.capability.router import CapabilityCard, CapabilitySearchHit
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.subagents.models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
)
from agent_py_agent.agent.subagents.policies import (
    _action_for_issue,
    _capability_hit_is_confident,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _risk_weight,
    _route_card_payload,
    _select_capability_hits,
    _severity_weight,
    filter_board_items,
)
from agent_py_agent.agent.subagents.reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem

# ── filter_board_items 测试 ────────────────────────────────────────────────

def test_filter_board_items_by_status():
    """测试按状态过滤。"""
    items = [
        SubAgentBoardItem(
            id="r1", root_id="root1", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
        SubAgentBoardItem(
            id="r2", root_id="root1", parent_id="", depth=0,
            status="DONE", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
    ]
    filtered = filter_board_items(items, status="running")
    assert len(filtered) == 1
    assert filtered[0].id == "r1"


def test_filter_board_items_by_owner():
    """测试按所有者过滤。"""
    items = [
        SubAgentBoardItem(
            id="r1", root_id="root1", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="alice", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
        SubAgentBoardItem(
            id="r2", root_id="root1", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="bob", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
    ]
    filtered = filter_board_items(items, owner="alice")
    assert len(filtered) == 1


def test_filter_board_items_by_root_id():
    """测试按根任务 ID 过滤。"""
    items = [
        SubAgentBoardItem(
            id="r1", root_id="root-a", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
        SubAgentBoardItem(
            id="r2", root_id="root-b", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
    ]
    filtered = filter_board_items(items, root_id="root-a")
    assert len(filtered) == 1
    assert filtered[0].id == "r1"


def test_filter_board_items_no_filter():
    """测试无过滤条件返回全部。"""
    items = [
        SubAgentBoardItem(
            id="r1", root_id="root1", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
        SubAgentBoardItem(
            id="r2", root_id="root1", parent_id="", depth=0,
            status="DONE", verification_status="UNVERIFIED", channel_status="OK",
            owner="u2", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
    ]
    filtered = filter_board_items(items)
    assert len(filtered) == 2


# ── _filter_action_plan_items 测试 ────────────────────────────────────────

def test_filter_action_plan_items_by_action():
    """测试按动作类型过滤。"""
    items = [
        ActionPlanItem(
            id="a1", run_id="r1", severity="P1", priority=500,
            action="probe_or_repair_channel", reason="test", source_issue_kinds=["channel_broken"],
        ),
        ActionPlanItem(
            id="a2", run_id="r2", severity="P1", priority=500,
            action="run_acceptance", reason="test", source_issue_kinds=["unverified_done"],
        ),
    ]
    filtered = _filter_action_plan_items(items, action_filter="probe_or_repair_channel")
    assert len(filtered) == 1


def test_filter_action_plan_items_by_run_id():
    """测试按 run_id 过滤。"""
    items = [
        ActionPlanItem(
            id="a1", run_id="run-1", severity="P1", priority=500,
            action="a", reason="test", source_issue_kinds=["test"],
        ),
        ActionPlanItem(
            id="a2", run_id="run-2", severity="P1", priority=500,
            action="b", reason="test", source_issue_kinds=["test"],
        ),
    ]
    filtered = _filter_action_plan_items(items, run_id="run-1")
    assert len(filtered) == 1


def test_filter_action_plan_items_with_limit():
    """测试结果数量限制。"""
    items = [
        ActionPlanItem(
            id=f"a{i}", run_id=f"r{i}", severity="P1", priority=500,
            action="probe", reason="test", source_issue_kinds=["test"],
        )
        for i in range(10)
    ]
    filtered = _filter_action_plan_items(items, limit=3)
    assert len(filtered) == 3


# ── _risk_weight 测试 ──────────────────────────────────────────────────────

def test_risk_weight_highest_failed():
    """测试 failed 标志权重最高。"""
    weight = _risk_weight(["failed"])
    assert weight == 100


def test_risk_weight_timeout():
    """测试 timeout 权重。"""
    weight = _risk_weight(["timeout"])
    assert weight == 95


def test_risk_weight_channel_error():
    """测试 channel_error 权重。"""
    weight = _risk_weight(["channel_error"])
    assert weight == 90


def test_risk_weight_multiple():
    """测试多标志取最大值。"""
    weight = _risk_weight(["open_capability_request", "failed"])
    assert weight == 100


def test_risk_weight_unknown_flag():
    """测试未知标志返回最小权重。"""
    weight = _risk_weight(["unknown_flag"])
    assert weight == 1


def test_risk_weight_empty_list():
    """测试空列表返回默认值。"""
    weight = _risk_weight([])
    assert weight == 0


# ── _severity_weight 测试 ─────────────────────────────────────────────────

def test_severity_weight_p0():
    """测试 P0 最高权重。"""
    assert _severity_weight("P0") == 1000


def test_severity_weight_p1():
    """测试 P1 权重。"""
    assert _severity_weight("P1") == 500


def test_severity_weight_p2():
    """测试 P2 权重。"""
    assert _severity_weight("P2") == 100


def test_severity_weight_unknown():
    """测试未知严重度返回 0。"""
    assert _severity_weight("P3") == 0


# ── _issue_weight 测试 ─────────────────────────────────────────────────────

def test_issue_weight_combines_severity_and_kind():
    """测试 issue 权重结合严重度和类型。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="missing_work_order_files",
        message="",
        suggested_action="",
    )
    weight = _issue_weight(issue)
    assert weight >= 1000 + 90  # P0 + missing_work_order_files


# ── _action_for_issue 测试 ─────────────────────────────────────────────────

def test_action_for_issue_channel_broken():
    """测试通道损坏问题的动作。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="channel_broken",
        message="",
        suggested_action="",
    )
    action, priority, status = _action_for_issue(issue)
    assert action == "probe_or_repair_channel"
    assert priority == 980


def test_action_for_issue_missing_work_order():
    """测试工单文件缺失的动作。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P0",
        kind="missing_work_order_files",
        message="",
        suggested_action="",
    )
    action, priority, status = _action_for_issue(issue)
    assert action == "repair_work_order"
    assert status == "BLOCKED"


def test_action_for_issue_unknown():
    """测试未知问题类型返回默认值。"""
    issue = DueCheckIssue(
        run_id="r1",
        severity="P1",
        kind="unknown_kind",
        message="",
        suggested_action="custom_action",
    )
    action, priority, status = _action_for_issue(issue)
    assert action == "custom_action"


# ── _commands_for_action 测试 ──────────────────────────────────────────────

def test_commands_for_action_probe():
    """测试探针动作的命令。"""
    commands = _commands_for_action("probe_or_repair_channel", "run-123")
    assert len(commands) >= 1
    assert "run-123" in commands[0]


def test_commands_for_action_unknown():
    """测试未知动作返回通用命令。"""
    commands = _commands_for_action("unknown_action", "run-456")
    assert len(commands) >= 1
    assert "run-456" in commands[0]


# ── _capability_request_query 测试 ────────────────────────────────────────

def test_capability_request_query_basic():
    """测试能力请求查询构建。"""
    task = SubAgentTask(
        id="t1",
        goal="分析销售数据",
        thought="需要分析数据",
        plan=["步骤1", "步骤2"],
    )
    request = CapabilityRequest(
        id="req1",
        from_run_id="t1",
        needed_capability="数据分析",
        problem="需要统计",
        expected_output="报表",
        tried=["尝试A"],
        evidence=[],
        constraints={},
    )
    query = _capability_request_query(task, request)
    assert "分析销售数据" in query
    assert "数据分析" in query


def test_capability_request_query_excludes_empty():
    """测试空字段不包含在查询中。"""
    task = SubAgentTask(
        id="t1",
        goal="目标",
        thought="",
        plan=[],
    )
    request = CapabilityRequest(
        id="req1",
        from_run_id="t1",
        needed_capability="能力",
        problem="",
        expected_output="",
        tried=[],
        evidence=[],
        constraints={},
    )
    query = _capability_request_query(task, request)
    # 只有 goal 和 needed_capability 有值
    lines = [l for l in query.split("\n") if l]
    assert len(lines) == 2


# ── _capability_hit_is_confident 测试 ──────────────────────────────────────

def test_capability_hit_is_confident_high_score():
    """测试高分命中被认为是可信的。"""
    card = CapabilityCard(id="c1", kind="skill", name="n", description="d")
    hit = CapabilitySearchHit(card=card, score=5.0, reasons=["kw"])
    assert _capability_hit_is_confident(hit) is True


def test_capability_hit_is_confident_low_score():
    """测试低分命中不被信任。"""
    card = CapabilityCard(id="c1", kind="skill", name="n", description="d")
    hit = CapabilitySearchHit(card=card, score=3.0, reasons=["kw"])
    assert _capability_hit_is_confident(hit) is False


# ── _select_capability_hits 测试 ───────────────────────────────────────────

def test_select_capability_hits_respects_limits():
    """测试选择命中时遵守数量限制。"""
    config = CapabilityConfig(capability_grant_max_skills=1, capability_grant_max_tools=1)

    card1 = CapabilityCard(id="s1", kind="skill", name="技能1", description="")
    card2 = CapabilityCard(id="s2", kind="skill", name="技能2", description="")
    card3 = CapabilityCard(id="t1", kind="tool", name="工具1", description="")

    hits = [
        CapabilitySearchHit(card=card1, score=10.0, reasons=["kw"]),
        CapabilitySearchHit(card=card2, score=10.0, reasons=["kw"]),
        CapabilitySearchHit(card=card3, score=10.0, reasons=["kw"]),
    ]

    selected = _select_capability_hits(hits, config)
    assert len(selected) <= 2  # 1 skill + 1 tool


def test_select_capability_hits_filters_low_confidence():
    """测试低置信度命中被过滤。"""
    config = CapabilityConfig()
    card = CapabilityCard(id="c1", kind="skill", name="n", description="")
    hit = CapabilitySearchHit(card=card, score=2.0, reasons=[])
    selected = _select_capability_hits([hit], config)
    assert len(selected) == 0


# ── _route_card_payload 测试 ──────────────────────────────────────────────

def test_route_card_payload_basic():
    """测试路由卡片载荷构建。"""
    card = CapabilityCard(
        id="card-1",
        kind="skill",
        name="测试技能",
        description="A" * 300,  # 超过 240 字符
        risk_level="medium",
        source="test",
        path="/path/to/skill",
    )
    hit = CapabilitySearchHit(card=card, score=8.5, reasons=["命中1", "命中2"])
    payload = _route_card_payload(hit)

    assert payload["id"] == "card-1"
    assert payload["kind"] == "skill"
    assert payload["name"] == "测试技能"
    assert len(payload["description"]) <= 240
    assert payload["risk_level"] == "medium"
    assert payload["score"] == "8.50"


# ── _dedupe_granted_cards 测试 ─────────────────────────────────────────────

def test_dedupe_granted_cards_removes_duplicates():
    """测试去重卡片。"""
    from agent_py_agent.agent.subagents.models import CapabilityGrant

    grant = CapabilityGrant(
        id="grant1",
        request_id="req1",
        grant_to_run_id="r1",
        capability_cards=[
            {"id": "c1", "kind": "skill", "name": "技能1"},
            {"id": "c1", "kind": "skill", "name": "技能1"},  # 重复
            {"id": "c2", "kind": "tool", "name": "工具1"},
        ],
    )
    cards = _dedupe_granted_cards([grant])
    assert len(cards) == 2


def test_dedupe_granted_cards_respects_max():
    """测试 max_cards 限制。"""
    from agent_py_agent.agent.subagents.models import CapabilityGrant

    grant = CapabilityGrant(
        id="grant1",
        request_id="req1",
        grant_to_run_id="r1",
        capability_cards=[
            {"id": f"c{i}", "kind": "skill", "name": f"s{i}"}
            for i in range(10)
        ],
    )
    cards = _dedupe_granted_cards([grant], max_cards=3)
    assert len(cards) == 3


# ── _execution_context_instructions 测试 ─────────────────────────────────

def test_execution_context_instructions_not_empty():
    """测试执行上下文指令非空。"""
    instructions = _execution_context_instructions()
    assert len(instructions) > 0
    assert any("allowed_skills" in i for i in instructions)


# ── _is_active 测试 ────────────────────────────────────────────────────────

def test_is_active_terminal_status():
    """测试终态状态不是 active。"""
    terminal_statuses = ["DONE", "FAILED", "BLOCKED", "DONE", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"]
    for status in terminal_statuses:
        assert _is_active(status) is False


def test_is_active_running():
    """测试运行状态是 active。"""
    assert _is_active("RUNNING") is True
    assert _is_active("PENDING") is True


# ── _default_forbidden_write_roots 测试 ────────────────────────────────────

def test_default_forbidden_write_roots_contains_home():
    """测试包含主目录。"""
    roots = _default_forbidden_write_roots()
    assert len(roots) >= 4
    assert any(str(Path.home()) in r for r in roots)


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_filter_board_items_mixed_inspect_collaboration():
    """测试状态过滤大小写敏感。"""
    items = [
        SubAgentBoardItem(
            id="r1", root_id="root1", parent_id="", depth=0,
            status="RUNNING", verification_status="UNVERIFIED", channel_status="OK",
            owner="u1", supervisor="", final_owner="", goal="",
            updated_at=0, heartbeat_at=0, evidence_count=0,
            open_request_count=0, open_gap_count=0, child_count=0,
            takeover_by="", locked_file_count=0, risk_flags=[],
            task_root="", final_report_ref="",
        ),
    ]
    filtered = filter_board_items(items, status="running")
    assert len(filtered) == 1